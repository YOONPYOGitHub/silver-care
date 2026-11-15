import time
from pathlib import Path
from typing import TypedDict, Unpack
from urllib.parse import urlsplit

import httpx
from azure.identity.aio import DefaultAzureCredential
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .grading import JUDGE_PROMPT, judge_input
from .models import (
    Captured,
    CapturedTrace,
    CaptureOutcome,
    Case,
    Dataset,
    Judgment,
    ServiceAnswer,
    fingerprint,
)


class OutputPart(BaseModel):
    type: str
    text: str = ""


class OutputItem(BaseModel):
    content: list[OutputPart] = Field(default_factory=list)


class ModelResponse(BaseModel):
    status: str
    model: str = Field(min_length=1)
    output: list[OutputItem]


class SettingsValues(TypedDict, total=False):
    judge_endpoint: str
    judge_deployment: str
    target_url: str
    timeout_seconds: float
    trace_token: SecretStr | str


class EvaluationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EVAL_",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )
    judge_endpoint: str = ""
    judge_deployment: str = ""
    target_url: str = ""
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    trace_token: SecretStr = Field(default=SecretStr(""), validation_alias="EVALUATION_TRACE_TOKEN")

    def __init__(self, *, env_file: Path | None = None, **values: Unpack[SettingsValues]) -> None:
        super().__init__(_env_file=env_file, **values)

    @field_validator("judge_endpoint", "target_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        ):
            raise ValueError("Use HTTPS without credentials/query, or HTTP on localhost")
        return value.rstrip("/")

    @field_validator("judge_endpoint")
    @classmethod
    def azure_token_origin(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if not (parsed.hostname or "").endswith(".openai.azure.com") or parsed.path:
                raise ValueError("Judge endpoint must be an Azure OpenAI account root URL")
        return value


async def collect_one(
    client: httpx.AsyncClient,
    base_url: str,
    case: Case,
    *,
    trace_token: str | None = None,
) -> Captured:
    started = time.monotonic()
    request_hash = fingerprint(case.request_body())
    headers = {}
    if trace_token is not None:
        if len(trace_token) < 32:
            raise ValueError("Evaluation traces require a separate token of at least 32 characters")
        headers = {"X-Ondam-Evaluation": "trace", "Authorization": f"Bearer {trace_token}"}
    try:
        response = await client.post(
            f"{base_url}/api/chat",
            json=case.request_body(),
            headers=headers,
        )
    except httpx.RequestError as exc:
        return Captured(
            case_id=case.id,
            request_sha256=request_hash,
            outcome="transport_error",
            status_code=None,
            answer=None,
            error=type(exc).__name__,
            latency_ms=int((time.monotonic() - started) * 1000),
            trace_status="error" if trace_token is not None else "not_requested",
            trace_error="No server trace: transport error" if trace_token is not None else None,
        )
    elapsed = int((time.monotonic() - started) * 1000)
    answer = None
    error = None
    outcome: CaptureOutcome = "answer"
    payload = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.is_success:
        try:
            answer = ServiceAnswer.model_validate(payload)
        except (ValueError, ValidationError):
            outcome, error = "invalid_response", "Response is not a valid chat answer"
    else:
        outcome, error = "http_error", f"HTTP {response.status_code}"
        if response.status_code == 422:
            if isinstance(payload, dict) and payload.get("code") == "safety_refusal":
                outcome = "safety_refusal"
    captured_trace = None
    trace_error = None
    if trace_token is not None:
        try:
            if not isinstance(payload, dict) or "evaluation_trace" not in payload:
                raise ValueError("Server did not return an evaluation trace")
            captured_trace = CapturedTrace.model_validate(payload["evaluation_trace"])
            accepted_request = {**case.request_body(), "message": case.question.strip()}
            if captured_trace.request_sha256 != fingerprint(accepted_request):
                raise ValueError("Server trace belongs to a different question/history")
        except ValueError as exc:
            trace_error = str(exc)
            captured_trace = None
    return Captured(
        case_id=case.id,
        request_sha256=request_hash,
        outcome=outcome,
        status_code=response.status_code,
        answer=answer,
        error=error,
        latency_ms=elapsed,
        trace_status=(
            "not_requested" if trace_token is None else "error" if trace_error else "captured"
        ),
        evaluation_trace=captured_trace,
        trace_error=trace_error,
    )


class Judge:
    def __init__(
        self,
        settings: EvaluationSettings,
        client: httpx.AsyncClient,
        credential: DefaultAzureCredential,
    ) -> None:
        if not settings.judge_endpoint or not settings.judge_deployment:
            raise ValueError("Set EVAL_JUDGE_ENDPOINT and EVAL_JUDGE_DEPLOYMENT")
        self.settings = settings
        self.client = client
        self.credential = credential
        self.actual_models: set[str] = set()

    async def grade(self, case: Case, dataset: Dataset, answer: ServiceAnswer) -> Judgment:
        token = await self.credential.get_token("https://cognitiveservices.azure.com/.default")
        response = await self.client.post(
            f"{self.settings.judge_endpoint}/openai/v1/responses",
            headers={"Authorization": f"Bearer {token.token}"},
            json={
                "model": self.settings.judge_deployment,
                "instructions": JUDGE_PROMPT,
                "input": judge_input(case, dataset, answer),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "reference_judgment",
                        "strict": True,
                        "schema": Judgment.model_json_schema(),
                    }
                },
                "max_output_tokens": 6000,
                "store": False,
            },
        )
        response.raise_for_status()
        payload = ModelResponse.model_validate(response.json())
        if payload.status != "completed":
            raise ValueError("Judge response is incomplete or invalid")
        self.actual_models.add(payload.model)
        parts = [
            part.text
            for item in payload.output
            for part in item.content
            if part.type == "output_text"
        ]
        if len(parts) != 1:
            raise ValueError("Expected one structured judge output, not a refusal")
        return Judgment.model_validate_json(parts[0])
