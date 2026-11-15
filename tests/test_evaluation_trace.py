import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from silvercare.app import create_app
from silvercare.azure import UpstreamError
from silvercare.config import Settings
from silvercare.models import ChatRequest, ChatResponse
from silvercare.tracing import EvaluationTrace
from tests.test_azure import make_services

TOKEN = "synthetic-evaluation-only-token-0123456789"


class TraceService:
    async def chat(self, payload, *, trace=None):
        if trace is not None:
            trace.data["route"] = "test"
            trace.data["model_reference_context"] = [{"text": "Public synthetic source."}]
        return ChatResponse(answer="테스트 답변", mode="grounded", notice="안내")

    async def close(self):
        pass


def settings(**values):
    return Settings(
        _env_file=None,
        evaluation_trace_enabled=True,
        evaluation_trace_token=SecretStr(TOKEN),
        **values,
    )


@pytest.mark.parametrize("authorization", ["", "Bearer wrong"])
def test_trace_requires_separate_authorization(authorization):
    with TestClient(create_app(settings(), TraceService())) as client:
        response = client.post(
            "/api/chat",
            json={"message": "테스트 질문"},
            headers={"X-Ondam-Evaluation": "trace", "Authorization": authorization},
        )
    assert response.status_code == 403
    assert TOKEN not in response.text and "evaluation_trace" not in response.json()


def test_ordinary_response_and_public_configuration_never_contain_trace():
    with TestClient(create_app(settings(), TraceService())) as client:
        response = client.post("/api/chat", json={"message": "테스트 질문"})
        config = client.get("/api/config")
    assert response.status_code == 200 and "evaluation_trace" not in response.json()
    assert "evaluation_trace" not in config.text and TOKEN not in config.text


def test_valid_token_cannot_enable_tracing_when_server_flag_is_off():
    config = Settings(_env_file=None, evaluation_trace_token=SecretStr(TOKEN))
    with TestClient(create_app(config, TraceService())) as client:
        response = client.post(
            "/api/chat",
            json={"message": "테스트 질문"},
            headers={"X-Ondam-Evaluation": "trace", "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 403


def test_trace_token_required_when_enabled():
    with pytest.raises(ValidationError, match="separate token"):
        Settings(_env_file=None, evaluation_trace_enabled=True)


def test_authorized_trace_is_request_scoped_and_excludes_credentials():
    with TestClient(create_app(settings(), TraceService())) as client:
        first = client.post(
            "/api/chat",
            json={"message": "첫 질문"},
            headers={"X-Ondam-Evaluation": "trace", "Authorization": f"Bearer {TOKEN}"},
        )
        second = client.post(
            "/api/chat",
            json={"message": "두 번째 질문"},
            headers={"X-Ondam-Evaluation": "trace", "Authorization": f"Bearer {TOKEN}"},
        )
        normal = client.post("/api/chat", json={"message": "일반 질문"})
    assert first.status_code == second.status_code == normal.status_code == 200
    a = first.json()["evaluation_trace"]
    b = second.json()["evaluation_trace"]
    assert a["trace_id"] != b["trace_id"]
    assert a["data"]["question"] == "첫 질문"
    assert b["data"]["question"] == "두 번째 질문"
    assert TOKEN not in first.text and "evaluation_trace" not in normal.json()


def test_authorized_upstream_failure_preserves_partial_trace_without_secrets():
    class FailedService(TraceService):
        async def chat(self, payload, *, trace=None):
            trace.data["route"] = "retrieval_generation"
            raise UpstreamError("search", 503)

    with TestClient(create_app(settings(), FailedService())) as client:
        response = client.post(
            "/api/chat",
            json={"message": "테스트 질문"},
            headers={"X-Ondam-Evaluation": "trace", "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 503
    data = response.json()["evaluation_trace"]["data"]
    assert data["route"] == "retrieval_generation" and data["failure"]["http_status"] == 503
    assert TOKEN not in response.text


async def test_trace_records_actual_model_context_without_vectors_or_bearer_tokens():
    def handle(request):
        body = json.loads(request.content)
        if "/search?" in str(request.url):
            if "evidence_kind eq 'reference'" not in body["filter"]:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "unique-chunk-id",
                            "title": "Synthetic source",
                            "content": "Complete evidence.",
                            "source_url": "https://example.test",
                            "updated_at": "2026-11-14",
                            "dataset_id": "synthetic",
                            "source_record_id": "original-record",
                            "evidence_kind": "reference",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": "serving-model",
                "output": [{"content": [{"type": "output_text", "text": "근거 답변입니다.[1]"}]}],
            },
        )

    service = make_services(handle)
    payload = ChatRequest(message="건강 정보를 설명해 주세요")
    trace = EvaluationTrace.for_request(payload)
    try:
        await service.chat(payload, trace=trace)
        data = trace.export()["data"]
        assert data["generation_called"]
        assert data["model_reference_context"][0]["text"] == "Complete evidence."
        assert data["reference_identities"][0]["id"] == "unique-chunk-id"
        assert data["cited_numbers"] == [1]
        assert data["actual_generation_model"] == "serving-model"
        serialized = json.dumps(trace.export())
        assert "Authorization" not in serialized and "content_vector" not in serialized
    finally:
        await service.close()
