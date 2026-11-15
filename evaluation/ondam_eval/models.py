import hashlib
import json
from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator

Category = Literal[
    "emergency", "cognition", "falls", "nutrition_swallowing", "medication", "chronic_conditions"
]
Rating = Literal["pass", "partial", "fail", "unsafe", "needs_review"]
CaptureOutcome = Literal[
    "answer", "safety_refusal", "http_error", "transport_error", "invalid_response"
]
Dimension = Literal["accuracy", "completeness", "triage", "safety", "grounding"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Evidence(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    publisher: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    accessed_on: date
    license_note: str = Field(min_length=1)
    excerpt: str = Field(min_length=40)


class Message(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class Criterion(StrictModel):
    id: str = Field(min_length=1)
    dimension: Dimension
    description: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    critical: bool


class Case(StrictModel):
    id: str = Field(min_length=1)
    category: Category
    risk: Literal["normal", "high"]
    question: str = Field(min_length=1, max_length=2000)
    history: list[Message] = Field(max_length=12)
    reference_answer: str = Field(min_length=1)
    required: list[Criterion] = Field(min_length=1)
    forbidden: list[Criterion] = Field(min_length=1)
    allowed_alternatives: list[str] = Field(min_length=1)
    review_status: Literal["draft", "expert_reviewed"]
    reviewed_by: str | None
    reviewed_at: date | None

    @model_validator(mode="after")
    def valid_review_and_ids(self) -> Self:
        for criteria in (self.required, self.forbidden):
            ids = [item.id for item in criteria]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{self.id}: duplicate criterion IDs")
        reviewed = self.review_status == "expert_reviewed"
        complete_review = bool(self.reviewed_by and self.reviewed_by.strip() and self.reviewed_at)
        if (reviewed and not complete_review) or (
            not reviewed and (self.reviewed_by is not None or self.reviewed_at is not None)
        ):
            raise ValueError(f"{self.id}: inconsistent expert review metadata")
        return self

    def request_body(self) -> dict[str, object]:
        return {"message": self.question, "history": [m.model_dump() for m in self.history]}


class Dataset(StrictModel):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sources: list[Evidence] = Field(min_length=1)
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_links(self) -> Self:
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("Duplicate evidence IDs")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case IDs")
        for case in self.cases:
            for item in case.required + case.forbidden:
                if not set(item.source_ids).issubset(source_ids):
                    raise ValueError(f"{case.id}/{item.id}: unresolved evidence ID")
        return self


class Assessment(StrictModel):
    criterion_id: str
    verdict: Literal["met", "partial", "missing", "contradicted", "uncertain", "present", "absent"]
    quote: str
    reason: str = Field(min_length=1)


class AdditionalClaim(StrictModel):
    claim: str = Field(min_length=1)
    verdict: Literal["supported", "contradicted", "not_assessable"]
    quote: str = Field(min_length=1)
    source_ids: list[str]
    reason: str = Field(min_length=1)


class Judgment(StrictModel):
    case_id: str
    required: list[Assessment]
    forbidden: list[Assessment]
    additional_claims: list[AdditionalClaim]


class ServiceAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    answer: str = Field(min_length=1)
    sources: list[dict[str, object]] = Field(default_factory=list)
    mode: Literal["grounded", "conversation", "emergency"]
    notice: str = ""
    medical_caution: str | None = None
    display_notice: str = ""

    def visible_text(self) -> str:
        notice = self.display_notice or self.medical_caution or self.notice
        return self.answer + (f"\n\n[Displayed notice]\n{notice}" if notice else "")


class CapturedTrace(StrictModel):
    schema_version: Literal["ondam-evaluation-trace-v1"]
    trace_id: UUID
    request_sha256: str
    data: dict[str, JsonValue]


class Captured(StrictModel):
    case_id: str
    request_sha256: str
    outcome: CaptureOutcome
    status_code: int | None
    answer: ServiceAnswer | None
    error: str | None
    latency_ms: int = Field(ge=0)
    trace_status: Literal["not_requested", "captured", "error"] = "not_requested"
    evaluation_trace: CapturedTrace | None = None
    trace_error: str | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if (self.outcome == "answer") != (self.answer is not None):
            raise ValueError("Only successful captures may contain an answer")
        if self.trace_status == "captured":
            if self.evaluation_trace is None or self.trace_error is not None:
                raise ValueError("Captured trace metadata is inconsistent")
        elif self.evaluation_trace is not None or (
            (self.trace_status == "error") != bool(self.trace_error)
        ):
            raise ValueError("Trace status is inconsistent")
        return self


class CaptureRun(StrictModel):
    id: str
    created_at: str
    dataset_sha256: str
    target_url: str
    target_label: str
    records: list[Captured] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_records(self) -> Self:
        if len({r.case_id for r in self.records}) != len(self.records):
            raise ValueError("Duplicate captured case IDs")
        return self


class CalibrationSample(StrictModel):
    id: str
    case_id: str
    answer: str
    expected: Rating
    reason: str


class Calibration(StrictModel):
    id: str
    version: str
    samples: list[CalibrationSample] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_samples(self) -> Self:
        if len({s.id for s in self.samples}) != len(self.samples):
            raise ValueError("Duplicate calibration sample IDs")
        return self


def fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
