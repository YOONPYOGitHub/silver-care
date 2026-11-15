"""Request-scoped diagnostics returned only to an authenticated evaluation client."""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from silvercare.models import ChatRequest
from silvercare.privacy import redact


def request_fingerprint(payload: ChatRequest) -> str:
    value = json.dumps(
        payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(value.encode()).hexdigest()


def source_identity(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "dataset_id",
        "title",
        "source_url",
        "source_record_id",
        "parent_id",
        "chunk_index",
        "source_type",
        "source_language",
        "jurisdiction",
        "audience",
        "topic",
        "review_status",
        "context_chunk_ids",
        "context_record_ids",
        "updated_at",
        "source_year",
        "risk_level",
        "evidence_kind",
    )
    return {
        **{key: row[key] for key in keys if key in row},
        "search_score": row.get("@search.score"),
        "reranker_score": row.get("@search.rerankerScore"),
    }


@dataclass
class EvaluationTrace:
    request_sha256: str
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    data: dict[str, Any] = field(default_factory=dict)
    searches: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def for_request(cls, payload: ChatRequest) -> "EvaluationTrace":
        return cls(
            request_sha256=request_fingerprint(payload),
            data={
                "question": redact(payload.message),
                "history": [
                    {"role": message.role, "content": redact(message.content)}
                    for message in payload.history
                ],
                "generation_called": False,
            },
        )

    def export(self) -> dict[str, Any]:
        return {
            "schema_version": "ondam-evaluation-trace-v1",
            "trace_id": self.trace_id,
            "request_sha256": self.request_sha256,
            "data": {**self.data, "searches": self.searches},
        }
