"""Internal Foundry adapter shared by comparison apps and offline-index preparation."""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from tokenizers import Tokenizer  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class ManagedSafetyRefusal(Exception):
    pass


class FoundryGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FOUNDRY_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    endpoint: str = ""
    llm_deployment: str = ""
    embedding_deployment: str = ""
    token_scope: str = "https://cognitiveservices.azure.com/.default"
    timeout: int = Field(default=600, ge=30, le=900)
    embedding_tokenizer_path: str = "/app/models/qwen3-embedding-tokenizer.json"

    @model_validator(mode="after")
    def valid_endpoint(self) -> "FoundryGatewaySettings":
        if self.endpoint:
            url = urlsplit(self.endpoint)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
                or not url.hostname.endswith(
                    (".services.ai.azure.com", ".cognitiveservices.azure.com", ".openai.azure.com")
                )
            ):
                raise ValueError("Foundry gateway requires an Azure HTTPS account endpoint")
        return self


def create_gateway(
    settings: FoundryGatewaySettings | None = None,
    client: httpx.AsyncClient | None = None,
    credential: DefaultAzureCredential | None = None,
    tokenizer: Tokenizer | None = None,
) -> FastAPI:
    config = settings or FoundryGatewaySettings()
    if not all((config.endpoint, config.llm_deployment, config.embedding_deployment)):
        raise ValueError("Foundry gateway endpoints/deployment names are required")
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout, connect=15))
    auth = credential or DefaultAzureCredential(exclude_interactive_browser_credential=True)
    encoder = tokenizer or Tokenizer.from_file(config.embedding_tokenizer_path)
    encoder.no_truncation()
    encoder.no_padding()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()
            await auth.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(ManagedSafetyRefusal)
    async def safety_refusal(request: Any, error: ManagedSafetyRefusal) -> JSONResponse:
        return JSONResponse({"error": {"code": "content_filter"}}, status_code=400)

    async def forward(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            token = await auth.get_token(config.token_scope)
            response = await http.post(
                config.endpoint.rstrip("/") + path,
                headers={"Authorization": f"Bearer {token.token}"},
                json=payload,
            )
        except (httpx.RequestError, ClientAuthenticationError) as error:
            logger.warning("Foundry gateway unavailable: %s", type(error).__name__)
            raise HTTPException(503, "Managed model service is unavailable.") from None
        if not response.is_success:
            logger.warning("Foundry gateway upstream HTTP %s", response.status_code)
            if response.status_code == 400:
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    if body["error"].get("code") == "content_filter":
                        raise ManagedSafetyRefusal
            raise HTTPException(503, "Managed model service rejected the request.")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected object")
        except ValueError:
            logger.warning("Foundry gateway returned invalid JSON")
            raise HTTPException(502, "Invalid managed model response.") from None
        return data

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def complete(payload: dict[str, Any]) -> JSONResponse:
        if payload.get("model") != "qwen3.5-4b" or not isinstance(payload.get("messages"), list):
            raise HTTPException(422, "Unsupported model alias or message format.")
        data = await forward(
            f"/managed-deployments/{config.llm_deployment}/v1/chat/completions",
            {**payload, "model": config.llm_deployment, "stream": False},
        )
        return JSONResponse(data)

    @app.post("/embed")
    async def embed(payload: dict[str, Any]) -> list[list[float]]:
        inputs = payload.get("inputs")
        texts = [inputs] if isinstance(inputs, str) else inputs
        if (
            not isinstance(texts, list)
            or not texts
            or len(texts) > 64
            or any(not isinstance(text, str) or not text.strip() for text in texts)
            or payload.get("truncate") is not False
            or payload.get("input_type", "document") not in {"query", "document"}
        ):
            raise HTTPException(422, "Expected bounded, nontruncating text inputs.")
        if payload.get("input_type") == "query":
            texts = [
                "Instruct: Retrieve relevant medical, care and welfare information.\nQuery: " + text
                for text in texts
            ]
        encodings = await asyncio.to_thread(encoder.encode_batch, texts)
        token_counts = [len(encoding.ids) for encoding in encodings]
        # This managed TEI template silently truncates at 16384, even with truncate=false.
        if any(count > 16384 for count in token_counts):
            raise HTTPException(422, "Embedding input exceeds the nontruncating 16384-token limit.")
        data = await forward(
            f"/managed-deployments/{config.embedding_deployment}/v1/embeddings",
            {
                "model": config.embedding_deployment,
                "input": texts,
                "dimensions": 1024,
                "encoding_format": "float",
            },
        )
        try:
            usage = data.get("usage")
            if not isinstance(usage, dict) or usage.get("prompt_tokens") != sum(token_counts):
                raise ValueError("Embedding token count changed")
            rows = data["data"]
            if not isinstance(rows, list) or len(rows) != len(texts):
                raise ValueError("Embedding count")
            ordered = sorted(rows, key=lambda row: row["index"])
            if [row["index"] for row in ordered] != list(range(len(texts))):
                raise ValueError("Embedding indexes")
            vectors = [row["embedding"] for row in ordered]
            for vector in vectors:
                if not isinstance(vector, list) or len(vector) != 1024:
                    raise ValueError("Embedding dimension")
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in vector
                ):
                    raise ValueError("Embedding values")
        except (ValueError, KeyError, TypeError):
            logger.warning("Foundry gateway returned invalid embeddings")
            raise HTTPException(502, "Invalid managed embedding response.") from None
        result: list[list[float]] = vectors
        return result

    return app
