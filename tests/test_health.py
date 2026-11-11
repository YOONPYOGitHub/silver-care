import json
import logging
import ssl
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from silvercare.app import create_app
from silvercare.azure import AzureServices
from silvercare.config import Settings
from silvercare.health import (
    BY_ID,
    ENDPOINT,
    TOPICS,
    HealthService,
    HealthUnavailable,
    article_evidence,
    parse_article,
    select_topics,
    tls_context,
)
from silvercare.models import ChatRequest, ChatResponse, HealthArticle, HealthSection, Message
from tests.test_azure import TestServices

XML = """<?xml version="1.0" encoding="UTF-8"?>
<XML><HEAD><CODE>S001</CODE><MESSAGE>OK</MESSAGE></HEAD><svc>
<CNTNTSSJ>낙상</CNTNTSSJ><CNTNTS_SN>1743</CNTNTS_SN><cntntsClList>
<cntntsCl><CNTNTS_CL_NM>요약문</CNTNTS_CL_NM>
<CNTNTS_CL_CN><![CDATA[<p>합성 공식 원문입니다. 직접 보존합니다.</p>
<p>둘째 문단: 약물·검사도 원문 그대로예요.</p>
<script>UNSAFE_SCRIPT</script><img src="https://image.test/pic">
<table><tr><td>AMBIGUOUS_TABLE</td></tr></table>]]></CNTNTS_CL_CN></cntntsCl>
<cntntsCl><CNTNTS_CL_NM>그림</CNTNTS_CL_NM>
<CNTNTS_CL_CN>https://image.test/pic</CNTNTS_CL_CN></cntntsCl>
</cntntsClList></svc></XML>""".encode()


def test_original_text_preserves_words_and_omits_nontext_tables_and_scripts():
    article = parse_article(XML, BY_ID["1743"])
    assert len(article.sections) == 1
    assert "합성 공식 원문입니다. 직접 보존합니다." in article.sections[0].text
    assert "약물·검사도 원문 그대로예요." in article.sections[0].text
    assert article.topic.id == "1743"
    assert article.topic.url.startswith("https://health.kdca.go.kr/")
    assert "제4유형" in article.license_notice
    assert "요약·재작성한 답변이 아닙니다" in article.notice
    for excluded in ("UNSAFE_SCRIPT", "AMBIGUOUS_TABLE", "image.test"):
        assert excluded not in article.model_dump_json()


def test_original_blocks_with_same_provider_section_id_share_one_heading():
    data = (
        "<XML><HEAD><CODE>S001</CODE></HEAD><svc><CNTNTSSJ>낙상</CNTNTSSJ>"
        "<CNTNTS_SN>1743</CNTNTS_SN><cntntsClList>"
        "<cntntsCl><CNTNTS_CL_NM>개요</CNTNTS_CL_NM><CNTNTSCLSN>101</CNTNTSCLSN>"
        "<CNTNTS_CL_CN>첫째 원문 문단입니다.</CNTNTS_CL_CN></cntntsCl>"
        "<cntntsCl><CNTNTS_CL_NM>개요</CNTNTS_CL_NM><CNTNTSCLSN>101</CNTNTSCLSN>"
        "<CNTNTS_CL_CN>둘째 원문 문단입니다.</CNTNTS_CL_CN></cntntsCl>"
        "</cntntsClList></svc></XML>"
    ).encode()
    article = parse_article(data, BY_ID["1743"])
    assert len(article.sections) == 1
    assert article.sections[0].text == "첫째 원문 문단입니다.\n\n둘째 원문 문단입니다."


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"<html>not XML</html>", "invalid_xml"),
        (b"\xff", "invalid_xml"),
        (b"<!DOCTYPE XML><XML/>", "unsafe_xml"),
        (XML.replace(b"S001", b"E001"), "provider_denied"),
        (XML.replace(b"1743", b"6758"), "content_identity"),
        (XML.replace("낙상".encode(), "다른 제목".encode()), "content_identity"),
        (XML.replace(b"cntntsClList", b"emptyList"), "empty_content"),
        (b"x" * (1024 * 1024 + 1), "response_limit"),
    ],
)
def test_invalid_or_empty_provider_content_is_an_explicit_failure(data, reason):
    with pytest.raises(HealthUnavailable) as error:
        parse_article(data, BY_ID["1743"])
    assert error.value.reason == reason


def test_tls_compatibility_is_not_a_certificate_or_protocol_downgrade():
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.options & ssl.OP_LEGACY_SERVER_CONNECT


def test_every_selected_topic_is_public_but_empty_exercise_article_is_excluded():
    assert len(TOPICS) == len(BY_ID) == 21
    assert "6758" not in BY_ID
    assert all(topic.public().url.endswith(topic.id) for topic in TOPICS)


def test_topic_matching_does_not_diagnose_or_carry_over_unrelated_family_history():
    assert [topic.id for topic in select_topics("낙상이 걱정돼요", [])] == ["1743"]
    assert select_topics("병원 예약이 있어요", []) == []
    assert select_topics("오늘 즐거웠어요", []) == []
    prior = [Message(role="user", content="어머니 고혈압에 관해 궁금해요")]
    assert select_topics("이번에는 제 이야기예요. 친구들과 즐겁게 지내요", prior) == []
    assert [t.id for t in select_topics("좀 더 자세히 알려주세요", prior)] == ["6698"]


def test_generation_evidence_selects_relevant_bounded_paragraphs_not_just_article_start():
    article = HealthArticle(
        topic=BY_ID["1743"].public(),
        sections=[
            HealthSection(title="개요", text="일반적인 배경 설명입니다.\n" * 50),
            HealthSection(title="진료", text="진료할 때 무엇을 기록할지 설명합니다."),
            HealthSection(title="예방", text="낙상 예방에는 합성 생활환경 점검이 중요합니다."),
            HealthSection(title="기타", text="기타 본문입니다." * 3000),
        ],
        fetched_at="2026-11-11T00:00:00+00:00",
    )
    evidence = article_evidence(article, "집에서 낙상을 예방하려면 생활환경을 어떻게 점검하나요?")
    assert "합성 생활환경 점검" in evidence
    assert "일반적인 배경 설명입니다." not in evidence
    assert 0 < len(evidence) <= 4800


async def test_generator_combines_aihub_knowledge_and_kdca_body_with_distinct_citations():
    captured = []
    provider_calls = []

    def provider(request):
        provider_calls.append(request)
        assert set(request.url.params) == {"TOKEN", "cntntsSn"}
        assert "질문" not in str(request.url)
        return httpx.Response(201, content=XML)

    def handler(request):
        body = json.loads(request.content)
        if "/docs/search" in str(request.url):
            if "dialogue" in body["filter"]:
                return httpx.Response(200, json={"value": [{"content": "합성 AI Hub 대화 예시"}]})
            if "qa_case" in body["filter"]:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "content": (
                                    "질문·선택지:\n낙상 검사 문항\n라벨 정답:\n검증되지 않은 정답"
                                ),
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "title": "AI Hub 의학 원천",
                            "content": "합성 AI Hub 지식 근거",
                            "source_url": "https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71874",
                            "updated_at": "2026-11-09",
                            "evidence_kind": "dataset_knowledge",
                        }
                    ]
                },
            )
        captured.append(body)
        context = json.loads(body["instructions"].split("\n검색 데이터(JSON):\n")[1])
        assert len(context["reference"]) == 2
        assert context["reference"][0]["text"] == "합성 AI Hub 지식 근거"
        official = context["reference"][1]
        assert official["evidence_kind"] == "official_api"
        assert official["source_record_id"] == "1743"
        assert "둘째 문단: 약물·검사도 원문 그대로예요." in official["text"]
        assert context["dialogue"][0]["text"] == "합성 AI Hub 대화 예시"
        assert "검증되지 않은 정답" not in body["instructions"]
        assert "synthetic-kdca-key" not in body["instructions"]
        assert body["store"] is False
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": "기존 지식과 공식 건강정보를 참고한 답변입니다. [1][2]",
                            }
                        ]
                    }
                ],
            },
        )

    settings = Settings(
        _env_file=None,
        azure_search_endpoint="https://search.example",
        azure_search_case_index="cases",
        kdca_health_api_key=SecretStr("synthetic-kdca-key"),
    )
    health = HealthService(
        settings.kdca_health_api_key, httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    service = TestServices(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        health_service=health,
    )
    try:
        result = await service.chat(ChatRequest(message="낙상 검사에 대한 질문이에요."))
        assert result.mode == "grounded"
        assert [source.id for source in result.sources] == [1, 2]
        assert result.sources[0].evidence_kind == "dataset_knowledge"
        assert result.sources[1].evidence_kind == "official_api"
        assert result.sources[1].url == BY_ID["1743"].public().url
        assert result.sources[1].record_id == "1743"
        assert result.health_topics[0].id == "1743"
        assert "AI Hub 수록자료의 출처 링크" in result.notice
        assert len(captured) == len(provider_calls) == 1
        assert result.medical_caution
    finally:
        await service.close()
        await health.close()


@pytest.mark.parametrize("message", ["낙상 후 의식이 없어요", "갑자기 가슴이 아파요"])
async def test_emergencies_bypass_both_model_search_and_official_api(message):
    settings = Settings(_env_file=None, kdca_health_api_key=SecretStr("synthetic"))
    health = HealthService(
        settings.kdca_health_api_key,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("No official API wait during emergencies")
            )
        ),
    )
    service = TestServices(
        settings,
        health_service=health,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("No search/model calls during emergencies")
            )
        ),
    )
    try:
        result = await service.chat(ChatRequest(message=message))
        assert result.mode == "emergency" and not result.health_topics
    finally:
        await service.close()
        await health.close()


async def test_api_failure_cannot_be_reported_as_a_successfully_grounded_answer():
    settings = Settings(
        _env_file=None,
        azure_search_endpoint="https://search.example",
        kdca_health_api_key=SecretStr("synthetic"),
    )

    def azure_handler(request):
        assert "/docs/search" in str(request.url), "Must not generate after official API failure"
        return httpx.Response(200, json={"value": []})

    health = HealthService(
        settings.kdca_health_api_key,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=XML.replace(b"cntntsClList", b"emptyList"))
            )
        ),
    )
    service = TestServices(
        settings,
        health_service=health,
        client=httpx.AsyncClient(transport=httpx.MockTransport(azure_handler)),
    )
    try:
        with pytest.raises(HealthUnavailable) as error:
            await service.chat(ChatRequest(message="낙상 예방 방법이 궁금해요."))
        assert error.value.reason == "empty_content"
    finally:
        await service.close()
        await health.close()


async def test_secret_only_goes_to_fixed_https_provider_and_is_removed_from_logs(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.copy_with(query=None) == httpx.URL(ENDPOINT)
        assert set(request.url.params) == {"TOKEN", "cntntsSn"}
        assert request.url.params["TOKEN"] == "synthetic+health/key"
        assert request.url.params["cntntsSn"] == "1743"
        return httpx.Response(201, content=XML)

    service = HealthService(
        SecretStr("synthetic%2Bhealth%2Fkey"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        first = await service.article("1743")
        second = await service.article("1743")
        assert first == second and len(calls) == 1
        assert "synthetic" not in first.model_dump_json()
        assert "synthetic" not in caplog.text
        assert "[REDACTED]" in caplog.text
    finally:
        await service.close()


async def test_expired_cache_does_not_mask_api_failure(caplog):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, content=XML) if calls == 1 else httpx.Response(503)

    service = HealthService(
        SecretStr("synthetic"), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        first = await service.article("1743")
        service.cache["1743"] = (time.monotonic() - 1, first)
        with pytest.raises(HealthUnavailable, match="unavailable"):
            await service.article("1743")
        assert calls == 2
        assert "provider_http" in caplog.text
    finally:
        await service.close()


async def test_redirect_is_not_followed_with_a_credential():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://other.example/collect"})

    service = HealthService(
        SecretStr("synthetic"), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(HealthUnavailable) as error:
            await service.article("1743")
        assert error.value.reason == "provider_http"
    finally:
        await service.close()


def test_public_health_endpoint_returns_originals_and_never_uses_model():
    settings = Settings(
        _env_file=None, kdca_health_api_key=SecretStr("synthetic"), rate_limit_per_minute=120
    )
    azure = AsyncMock(spec=AzureServices)
    app = create_app(settings, azure)
    health = HealthService(
        settings.kdca_health_api_key,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(201, content=XML))
        ),
    )
    with TestClient(app) as client:
        app.state.health = health
        assert client.get("/api/config").json()["health_info_enabled"]
        topics = client.get("/api/health-topics")
        assert len(topics.json()) == 21
        response = client.get("/api/health-info/1743")
        assert response.status_code == 200
        assert response.json()["sections"][0]["title"] == "요약문"
        assert "synthetic" not in response.text + topics.text + client.get("/api/config").text
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/health-info/6758").status_code == 404
    azure.chat.assert_not_called()
    azure.search.assert_not_called()


def test_chat_exposes_topic_links_but_not_raw_bodies_and_emergencies_have_no_cards():
    settings = Settings(
        _env_file=None, kdca_health_api_key=SecretStr("synthetic"), rate_limit_per_minute=120
    )
    azure = AsyncMock(spec=AzureServices)
    azure.chat.return_value = ChatResponse(
        answer="일반 답변입니다.", mode="conversation", notice="AI 안내"
    )
    with TestClient(create_app(settings, azure)) as client:
        response = client.post("/api/chat", json={"message": "낙상에 대해 알려주세요."})
        assert response.json()["health_topics"][0]["id"] == "1743"
        assert "sections" not in json.dumps(response.json()["health_topics"])
        azure.chat.return_value = ChatResponse(
            answer="119로 직접 연락해 주세요.", mode="emergency", notice="응급 안내"
        )
        emergency = client.post("/api/chat", json={"message": "낙상 후 의식이 없어요."})
        assert emergency.json()["health_topics"] == []


def test_unconfigured_health_is_explicit_and_does_not_change_chat():
    with TestClient(create_app(Settings(_env_file=None), AsyncMock(spec=AzureServices))) as client:
        assert client.get("/api/health-topics").json() == []
        assert not client.get("/api/config").json()["health_info_enabled"]
        response = client.get("/api/health-info/1743")
        assert response.status_code == 503
        assert "공식 건강정보" in response.json()["detail"]
