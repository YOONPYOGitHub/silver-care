import json

import httpx
import pytest

from silvercare.azure import SYSTEM_PROMPT, AzureServices, UpstreamError
from silvercare.config import Settings
from silvercare.models import ChatRequest, Message
from silvercare.safety import MEDICAL_CAUTION, emergency_response, medical_caution


class TestServices(AzureServices):
    __test__ = False

    async def token(self, search: bool = False) -> str:
        return "synthetic-token"


def test_system_instructions_are_a_single_current_audience_policy():
    assert "별도 언급이 없으면 이용자 본인의 이야기로 답하세요." in SYSTEM_PROMPT
    assert "가장 최근 질문의 대상을 따르세요." in SYSTEM_PROMPT
    assert "직원의 업무를 이용자의 할 일로 바꾸지 마세요." in SYSTEM_PROMPT
    assert "질문의 복잡도, 이용자가 요청한 설명 수준과 대화 맥락" in SYSTEM_PROMPT
    assert "요청한 항목을 빠짐없이" in SYSTEM_PROMPT
    assert "단계·적용 조건·이유·구체적인 예시" in SYSTEM_PROMPT
    assert "이해와 실행에 필요한 설명을 생략하지" in SYSTEM_PROMPT
    assert "간단한 사실이나 연락처만" in SYSTEM_PROMPT
    assert "반드시 해당 official_api 자료의 인용 번호" in SYSTEM_PROMPT
    assert "실행할 일은 1~3가지" not in SYSTEM_PROMPT
    assert "2~4가지" not in SYSTEM_PROMPT
    assert "업데이트" not in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.count("당신은 ") == 1


def make_services(handler):
    return TestServices(
        Settings(
            _env_file=None,
            azure_search_endpoint="https://search.example",
            azure_speech_resource_id="/subscriptions/example/speech",
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "message",
    [
        "아버지가 숨을 못 쉬어요",
        "어머니 의식이 없어요",
        "갑자기 가슴이 아파요",
        "죽고 싶어요",
        "자살하고 싶어요",
        "쓰러졌어요",
        "호흡 곤란이 있어요",
    ],
)
def test_emergency_routing(message):
    response = emergency_response(message)
    assert response is not None
    assert response.mode == "emergency"
    assert "119" in response.answer
    assert "직접 전화" in response.answer
    assert response.medical_caution is None


@pytest.mark.parametrize(
    "message,answer",
    [
        ("약물의 부작용이 궁금해요", "일반적인 정보를 알려드릴게요."),
        ("혈액검사는 무엇을 확인하나요?", "의료진과 확인해 주세요."),
        ("약을 먹는 시간을 바꿔도 될까요?", "약사에게 상담해 주세요."),
        ("CT와 MRI가 다른가요?", "검사의 목적에 따라 달라요."),
        ("이거 좀 더 알려주세요", "복용량은 의료진과 확인해 주세요."),
        ("부모님이 당뇨가 있어요", "개인 상황에 맞게 상담해 주세요."),
    ],
)
def test_medical_caution_matches_question_or_answer(message, answer):
    assert medical_caution(message, answer) == MEDICAL_CAUTION


@pytest.mark.parametrize(
    "message,answer",
    [
        ("안녕하세요", "반갑습니다."),
        ("오늘 약속이 있어요", "즐거운 시간 보내세요."),
        ("상담 예약을 하고 싶어요", "기관에 문의해 주세요."),
        ("대화 내용을 요약해 주세요", "오늘 나눈 이야기입니다."),
    ],
)
def test_nonmedical_conversation_does_not_get_medical_caution(message, answer):
    assert medical_caution(message, answer) is None


async def test_medical_caution_is_returned_without_model_having_to_add_it():
    def handler(request):
        if "/docs/search" in str(request.url):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": "담당 의료진과 확인해 주세요."}]}
                ],
            },
        )

    service = make_services(handler)
    try:
        result = await service.chat(ChatRequest(message="검사 전 준비사항이 궁금해요"))
        assert result.medical_caution == MEDICAL_CAUTION
        assert "AI" in result.notice
    finally:
        await service.close()


async def test_emergency_bypasses_all_azure_calls():
    def handler(request):
        pytest.fail("Emergency responses must not depend on Azure availability")

    service = make_services(handler)
    try:
        response = await service.chat(ChatRequest(message="의식이 없어요"))
        assert response.mode == "emergency"
    finally:
        await service.close()


async def test_rag_citations_privacy_and_followup():
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append((request, payload))
        if "/docs/search" in str(request.url):
            if "dialogue" in payload["filter"]:
                return httpx.Response(200, json={"value": [{"content": "예시 말투"}]})
            assert "장기요양" in payload["search"]
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "title": "공식 안내",
                            "content": "공단에 문의해 주세요.",
                            "source_url": "https://www.longtermcare.or.kr/",
                            "updated_at": "2026-11-09",
                        }
                    ]
                },
            )
        assert payload["model"] == "gpt-6.1-terra"
        assert payload["store"] is False
        assert "010-1234-5678" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [{"type": "output_text", "text": "공단에 문의해 주세요. [1]"}],
                    }
                ],
            },
        )

    service = make_services(handler)
    try:
        response = await service.chat(
            ChatRequest(
                message="어디에 신청해요? 제 번호는 010-1234-5678",
                history=[Message(role="user", content="장기요양이 궁금해요")],
            )
        )
        assert response.mode == "grounded"
        assert len(response.sources) == 1
        assert len(calls) == 3
    finally:
        await service.close()


@pytest.mark.parametrize(
    "text,status",
    [("거짓 근거 [9]", "completed"), ("", "completed"), ("중간에 끝남", "incomplete")],
)
async def test_invalid_or_incomplete_model_response(text, status):
    def handler(request):
        if "/docs/search" in str(request.url):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(
            200,
            json={
                "status": status,
                "output": [{"content": [{"type": "output_text", "text": text}]}],
            },
        )

    service = make_services(handler)
    try:
        with pytest.raises(UpstreamError):
            await service.chat(ChatRequest(message="안녕하세요"))
    finally:
        await service.close()


async def test_search_failure_does_not_fall_back_to_ungrounded_success():
    service = make_services(lambda request: httpx.Response(403))
    try:
        with pytest.raises(UpstreamError, match="search"):
            await service.chat(ChatRequest(message="장기요양 알려줘"))
    finally:
        await service.close()


async def test_speech_xml_escaping_and_stt():
    def handler(request):
        if "/tts/" in str(request.url):
            assert b"&lt;fake&gt;&amp;" in request.content
            assert request.headers["authorization"].startswith("Bearer aad#")
            return httpx.Response(200, content=b"audio")
        return httpx.Response(200, json={"combinedPhrases": [{"text": "안녕하세요"}]})

    service = make_services(handler)
    try:
        assert await service.synthesize("<fake>&") == b"audio"
        assert await service.transcribe(b"audio", "audio/webm") == "안녕하세요"
    finally:
        await service.close()


async def test_speech_provider_receives_plain_body_not_markdown_or_source_footer():
    from xml.etree import ElementTree as ET

    from tests.test_speech_text import ANSWER

    calls = []

    def handler(request):
        calls.append(request)
        root = ET.fromstring(request.content)
        spoken = "".join(root.itertext())
        assert "통증이 심하다면 오늘 의료진에게 진료받아 보세요." in spoken
        assert "즉시 119에 연락해 도움을 요청하세요." in spoken
        assert spoken.endswith("허리 통증은 언제부터 시작됐나요?")
        for marker in ("**", "[5]", "출처:", "신경외과학회", "마미 증후군 자료"):
            assert marker not in spoken
        return httpx.Response(200, content=b"audio")

    service = make_services(handler)
    try:
        assert await service.synthesize(ANSWER) == b"audio"
        assert len(calls) == 1
    finally:
        await service.close()


async def test_dataset_knowledge_is_cited_with_clear_provenance_and_notice():
    def handler(request):
        body = json.loads(request.content)
        if "/docs/search" in str(request.url):
            if "dialogue" in body["filter"]:
                return httpx.Response(200, json={"value": []})
            assert "dataset_knowledge" in body["filter"]
            assert "review_status" in body["filter"]
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "title": "생활 자료 (AI Hub 수록자료)",
                            "content": "합성 테스트 자료입니다.",
                            "source_url": "https://aihub.or.kr/dataset",
                            "updated_at": "2026-11-09",
                            "evidence_kind": "dataset_knowledge",
                            "source_record_id": "test-1",
                            "source_display": "시험 출처",
                            "source_year": "2024",
                            "review_status": "automated",
                        }
                    ]
                },
            )
        assert "전문가 검토 전" in body["instructions"]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": "테스트 답변입니다. [1]"}]}
                ],
            },
        )

    service = make_services(handler)
    try:
        result = await service.chat(ChatRequest(message="생활 돌봄"))
        assert result.mode == "grounded"
        assert result.sources[0].record_id == "test-1"
        assert result.sources[0].source_year == "2024"
        assert "전문가 검토 전" in result.notice
        assert "데이터셋 안내" in result.notice
    finally:
        await service.close()


async def test_cited_who_source_exposes_original_and_adaptation_license_notices():
    def handler(request):
        body = json.loads(request.content)
        if "/docs/search" in str(request.url):
            if "dialogue" in body["filter"]:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "who",
                            "title": "WHO ICOPE",
                            "content": "Synthetic provider-facing integrated-care reference.",
                            "source_url": "https://www.who.int/publications/i/item/9789240103726",
                            "updated_at": "2025-09-22",
                            "evidence_kind": "reference",
                            "dataset_id": "who-icope-2025",
                            "license": "Original CC BY-NC-SA 3.0 IGO",
                            "audience": "provider",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": "근거를 확인한 안내입니다. [1]"}]}
                ],
            },
        )

    service = make_services(handler)
    service.settings.noncommercial_data_use = True
    try:
        result = await service.chat(ChatRequest(message="생활 지원 계획을 세우고 싶어요."))
        source = result.sources[0]
        assert source.license == "Original CC BY-NC-SA 3.0 IGO"
        assert source.license_url == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
        assert "not created by WHO" in source.reuse_notice
        assert "비상업적" in source.reuse_notice
        assert "original English" in source.reuse_notice
    finally:
        await service.close()


@pytest.mark.parametrize("kind", ["reference", "dataset_knowledge"])
async def test_medical_reply_adds_one_compact_display_notice_and_keeps_raw_api_guidance(kind):
    from silvercare.safety import MEDICAL_CAUTION, MEDICAL_DISPLAY_NOTICE

    def handler(request):
        body = json.loads(request.content)
        if "/docs/search" in str(request.url):
            if "dialogue" in body["filter"]:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "title": "Synthetic",
                            "content": "Synthetic factual reference.",
                            "source_url": "https://example.test/source",
                            "updated_at": "2026-11-14",
                            "evidence_kind": kind,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": "일반적인 검사 안내입니다. [1]"}]}
                ],
            },
        )

    service = make_services(handler)
    try:
        result = await service.chat(ChatRequest(message="검사 정보를 알고 싶어요."))
        assert result.medical_caution == MEDICAL_CAUTION
        assert result.display_notice.startswith(MEDICAL_DISPLAY_NOTICE)
        assert result.notice != result.display_notice
        if kind == "dataset_knowledge":
            assert "AI Hub" in result.notice
            assert "AI Hub" in result.display_notice
            assert "전문가 검토 전" in result.display_notice
        assert len(result.display_notice) < len(result.notice) + len(result.medical_caution)
    finally:
        await service.close()
