import json
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from silvercare.app import create_app
from silvercare.azure import UpstreamError
from silvercare.config import Settings
from silvercare.local_services import (
    LLM_ONLY_PROMPT,
    MODEL_ONLY_NOTICE,
    LocalServices,
    QwenServices,
)
from silvercare.models import ChatRequest, HealthArticle, HealthSection, HealthTopic
from silvercare.tracing import EvaluationTrace


def settings(variant="local-only", **kwargs):
    return Settings(
        _env_file=None,
        service_variant=variant,
        local_llm_endpoint="http://model:8080",
        local_search_endpoint="http://search:9200" if variant == "local-rag" else "",
        local_embedding_endpoint="http://embedding:8080" if variant == "local-rag" else "",
        local_reranker_endpoint="http://reranker:8080" if variant == "local-rag" else "",
        **kwargs,
    )


def completion(text="일반적인 정보를 설명합니다.", finish="stop"):
    return {
        "model": "qwen3.5-4b",
        "choices": [
            {
                "message": {"content": text},
                "finish_reason": finish,
            }
        ],
    }


def azure_settings(**kwargs):
    return settings(
        "qwen-azure-rag",
        azure_openai_endpoint="https://azure-model.test",
        azure_search_endpoint="https://azure-search.test",
        azure_search_index="existing-knowledge",
        azure_search_case_index="existing-cases",
        azure_embedding_deployment="existing-embedding",
        retrieval_mode="hybrid",
        **kwargs,
    )


async def test_qwen_azure_rag_uses_existing_azure_embeddings_search_and_kdca():
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        if request.url.host == "model":
            assert request.url.path == "/v1/chat/completions"
            assert body["model"] == "qwen3.5-4b"
            if body["max_tokens"] == 256:
                return httpx.Response(200, json=completion("낙상 예방 falls"))
            assert "Azure 검색 근거" in body["messages"][0]["content"]
            assert "질병관리청 본문" in body["messages"][0]["content"]
            return httpx.Response(200, json=completion("집 안의 위험 요인을 점검하세요.[1][2]"))
        if request.url.host == "azure-model.test":
            assert request.url.path == "/openai/v1/embeddings"
            assert body["model"] == "existing-embedding" and body["dimensions"] == 512
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 512}]})
        assert request.url.host == "azure-search.test"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert len(body["vectorQueries"][0]["vector"]) == 512
        if "dialogue" in body["filter"] or "qa_case" in body["filter"]:
            return httpx.Response(200, json={"value": []})
        assert "/indexes/existing-knowledge/docs/search" == request.url.path
        assert body["queryType"] == "semantic"
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "fact",
                        "title": "Azure 검색 근거",
                        "content": "공식 안내 본문입니다.",
                        "source_url": "https://example.test/guide",
                        "updated_at": "2026-01-01",
                        "dataset_id": "synthetic",
                    }
                ]
            },
        )

    credential = AsyncMock()
    credential.get_token.return_value = AccessToken("synthetic-token", 9999999999)
    service = QwenServices(
        azure_settings(
            rewrite_queries=True, semantic_ranking=True, kdca_health_api_key=SecretStr("synthetic")
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        credential=credential,
    )
    service.health.article = AsyncMock(
        return_value=HealthArticle(
            topic=HealthTopic(id="1743", title="낙상", url="https://health.kdca.go.kr/falls"),
            sections=[HealthSection(title="예방", text="질병관리청 본문에서 낙상을 예방합니다.")],
            fetched_at="2026-01-01T00:00:00Z",
            source_display="질병관리청",
        )
    )
    payload = ChatRequest(message="집에서 낙상을 예방하려면 어떻게 하나요?")
    trace = EvaluationTrace.for_request(payload)
    try:
        result = await service.chat(payload, trace=trace)
        assert len(result.sources) == 2 and result.mode == "grounded"
        assert any(row.evidence_kind == "official_api" for row in result.sources)
        assert trace.data["service_variant"] == "qwen-azure-rag"
        assert trace.data["settings"]["knowledge_index"] == "existing-knowledge"
        assert trace.data["core_execution"] == "foundry-small-model-with-azure-search"
        assert not any(r.url.host in {"search", "embedding", "reranker"} for r in calls)
        service.health.article.assert_awaited_once()
    finally:
        await service.close()


@pytest.mark.parametrize("count", [6562, 0, "invalid", None])
async def test_qwen_azure_readiness_checks_shared_gateway_and_existing_indexes(count):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=count if "$count" in request.url.path else {})

    credential = AsyncMock()
    credential.get_token.return_value = AccessToken("synthetic-token", 9999999999)
    service = QwenServices(
        azure_settings(),
        credential=credential,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        if count == 6562:
            await service.ready()
            assert paths == [
                "/health",
                "/indexes/existing-knowledge/docs/$count",
                "/indexes/existing-cases/docs/$count",
            ]
        else:
            with pytest.raises(UpstreamError):
                await service.ready()
    finally:
        await service.close()


def test_qwen_azure_factory_capabilities_and_prefix_are_independent():
    with TestClient(create_app(azure_settings(base_path="/qwen-azure-rag"))) as client:
        assert isinstance(client.app.state.azure, QwenServices)
        assert not isinstance(client.app.state.azure, LocalServices)
        config = client.get("/api/config").json()
        assert config["service_variant"] == "qwen-azure-rag" and config["search_enabled"]
        html = client.get("/").text
        assert 'src="/qwen-azure-rag/static/app.js"' in html
        assert 'data-base-path="/qwen-azure-rag"' in html


@pytest.mark.parametrize(
    "field", ["azure_search_endpoint", "azure_search_case_index", "azure_embedding_deployment"]
)
def test_qwen_azure_requires_azure_retrieval_configuration(field):
    values = azure_settings().model_dump()
    values[field] = ""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


async def test_model_only_has_exactly_one_local_call_no_hidden_evidence_or_query_rewrite():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url == "http://model:8080/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "qwen3.5-4b"
        assert body["messages"][0]["content"] == LLM_ONLY_PROMPT
        assert "검색 데이터(JSON)" not in body["messages"][0]["content"]
        assert "010-1234-5678" not in request.content.decode()
        return httpx.Response(200, json=completion("낙상 예방은 생활환경과 관련이 있습니다."))

    service = LocalServices(
        settings(kdca_health_api_key=SecretStr("synthetic-health-key")),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    service.health.article = AsyncMock(side_effect=AssertionError("No health grounding"))
    service.embed = AsyncMock(side_effect=AssertionError("No embedding"))
    service.search_documents = AsyncMock(side_effect=AssertionError("No search"))
    service.rewrite_query = AsyncMock(side_effect=AssertionError("No query rewriting"))
    trace = EvaluationTrace.for_request(ChatRequest(message="낙상을 예방하려면 어떻게 하나요?"))
    try:
        result = await service.chat(
            ChatRequest(message="낙상을 예방하려면 어떻게 하나요? 010-1234-5678"), trace=trace
        )
        assert len(calls) == 1
        assert result.sources == [] and result.health_topics == []
        assert "학습 지식" in result.notice
        assert trace.data["service_variant"] == "local-only"
        assert trace.data["model_reference_context"] == []
        assert trace.searches == []
    finally:
        await service.close()


async def test_local_rag_keeps_kdca_grounding_and_citation_handling():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "embedding":
            return httpx.Response(200, json=[[0.1] * 512])
        assert request.url.host == "model"
        body = json.loads(request.content)
        if body["max_tokens"] == 256:
            return httpx.Response(200, json=completion("낙상 예방 falls"))
        prompt = body["messages"][0]["content"]
        assert "기존 검색 근거" in prompt and "질병관리청 본문" in prompt
        assert "해당 자료 id를 [1] 형식으로 인용" in body["messages"][-1]["content"]
        return httpx.Response(200, json=completion("집 안의 위험 요인을 점검해 보세요.[1][2]"))

    service = LocalServices(
        settings(
            "local-rag",
            retrieval_mode="hybrid",
            rewrite_queries=True,
            semantic_ranking=True,
            kdca_health_api_key=SecretStr("synthetic"),
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def documents(index, payload):
        assert index.startswith("local-")
        assert "vectorQueries" in payload
        if "dialogue" in payload["filter"] or "qa_case" in payload["filter"]:
            return []
        return [
            {
                "id": "fact",
                "title": "기존 검색 근거",
                "content": "기존 검색 근거 본문입니다.",
                "source_url": "https://example.test/guide",
                "updated_at": "2026-01-01",
                "source_record_id": "fact",
                "dataset_id": "synthetic",
            }
        ]

    service.search_documents = documents
    service.health.article = AsyncMock(
        return_value=HealthArticle(
            topic=HealthTopic(id="1743", title="낙상", url="https://health.kdca.go.kr/falls"),
            sections=[HealthSection(title="예방", text="질병관리청 본문에서 낙상을 예방합니다.")],
            fetched_at="2026-01-01T00:00:00Z",
            source_display="질병관리청",
        )
    )
    try:
        result = await service.chat(ChatRequest(message="집에서 낙상을 예방하려면 어떻게 하나요?"))
        assert len(result.sources) == 2
        assert any(row.evidence_kind == "official_api" for row in result.sources)
        assert all(
            "openai.azure.com" not in url and "search.windows.net" not in url for url in calls
        )
        service.health.article.assert_awaited_once()
    finally:
        await service.close()


@pytest.mark.parametrize(
    "text,finish", [("", "stop"), ("잘린 응답", "length"), ("응답[1]", "stop")]
)
async def test_model_only_rejects_empty_incomplete_and_fabricated_citations(text, finish):
    service = LocalServices(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=completion(text, finish))
            )
        ),
    )
    try:
        with pytest.raises(UpstreamError):
            await service.chat(ChatRequest(message="생활 정보를 알려주세요."))
    finally:
        await service.close()


async def test_fixed_emergency_does_not_depend_on_local_model():
    service = LocalServices(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("No network for fixed emergency"))
        ),
    )
    try:
        result = await service.chat(ChatRequest(message="지금 숨을 못 쉬어요"))
        assert result.mode == "emergency"
    finally:
        await service.close()


@pytest.mark.parametrize("message", ["안녕하세요.", "낙상을 예방하려면 어떻게 하나요?"])
async def test_model_only_knowledge_notice_is_shown_once(message):
    service = LocalServices(
        settings(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
        ),
    )
    try:
        result = await service.chat(ChatRequest(message=message))
        assert result.notice.count(MODEL_ONLY_NOTICE) == 1
        assert result.display_notice.count(MODEL_ONLY_NOTICE) == 1
    finally:
        await service.close()


async def test_rag_casual_conversation_does_not_demand_nonexistent_citations():
    def handler(request):
        body = json.loads(request.content)
        assert body["messages"][-1]["content"] == "안녕하세요."
        return httpx.Response(200, json=completion("안녕하세요."))

    service = LocalServices(
        settings("local-rag"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await service.generate(
            '도우미입니다.\n검색 데이터(JSON):\n{"reference":[]}',
            [{"role": "user", "content": "안녕하세요."}],
            1300,
        )
    finally:
        await service.close()


async def test_readiness_does_not_queue_an_inference_behind_cpu_reranking():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"count": 1})

    service = LocalServices(
        settings("local-rag"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        await service.ready()
        assert paths == [
            "/health",
            "/local-knowledge-v1/_count",
            "/local-cases-v1/_count",
            "/health",
            "/info",
        ]
    finally:
        await service.close()


def test_experimental_base_path_and_capabilities_are_isolated():
    service = LocalServices(
        settings(base_path="/local-only", kdca_health_api_key=SecretStr("synthetic")),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("No model/search/health calls for viewing the app")
            )
        ),
    )
    with TestClient(create_app(service.settings, service)) as client:
        config = client.get("/api/config").json()
        assert config["service_variant"] == "local-only"
        assert config["search_enabled"] is False
        assert config["health_info_enabled"] is False
        assert config["request_timeout_ms"] == 600000
        assert client.get("/api/health-topics").json() == []
        assert client.get("/api/health-info/1743").status_code == 404
        html = client.get("/").text
        assert 'data-base-path="/local-only"' in html
        assert 'src="/local-only/static/app.js"' in html
        assert 'href="/local-only/static/styles.css"' in html
        assert 'href="/local-only/"' in html


@pytest.mark.parametrize("base", ["/../", "/local?x=1", "//outside", '"/unsafe'])
def test_base_paths_cannot_inject_or_escape_service_routes(base):
    with pytest.raises(ValidationError):
        settings(base_path=base)


def test_llm_only_prompt_is_not_an_artificial_refusal_policy():
    assert (
        "근거 문서가 제공되지 않았다는 이유만으로 일반적인 설명까지 거절하지 마세요."
        in LLM_ONLY_PROMPT
    )
    assert "reference 자료 범위에서만" not in LLM_ONLY_PROMPT
    assert "개인별 진단" in LLM_ONLY_PROMPT
    assert "답변의 분량과 구성" in LLM_ONLY_PROMPT
