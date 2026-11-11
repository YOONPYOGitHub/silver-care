from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from silvercare.app import RateLimiter, create_app
from silvercare.azure import AzureServices, UpstreamError
from silvercare.config import Settings
from silvercare.models import ChatResponse


@pytest.fixture
def config():
    return Settings(_env_file=None, rate_limit_per_minute=120)


@pytest.fixture
def services():
    service = AsyncMock(spec=AzureServices)
    service.chat.return_value = ChatResponse(
        answer="마음을 나눠 주세요.", mode="conversation", notice="의료진이 아닙니다."
    )
    service.synthesize.return_value = b"audio-bytes"
    service.transcribe.return_value = "안녕하세요."
    return service


def test_ui_and_headers(config, services):
    with TestClient(create_app(config, services)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "오늘도," in response.text
        assert 'id="mode-text"' in response.text
        assert 'id="mode-voice"' in response.text
        assert 'id="voice-panel"' in response.text
        assert 'id="voice-finish"' in response.text
        assert 'id="voice-pause"' in response.text
        assert 'id="stt-toggle"' not in response.text
        assert 'id="tts-toggle"' not in response.text
        assert 'id="record"' not in response.text
        assert "최대 45초" not in response.text
        assert "한 번에 최대 2분까지 말씀하실 수" in response.text
        assert "녹음" not in response.text
        assert "마이크 입력을 일시적으로 수집" in response.text
        settings_panel = response.text.split('id="voice-settings"', 1)[1].split("</section>", 1)[0]
        assert "글자 크기를 편하게 조절해 보세요." in settings_panel
        assert "글과 음성은 앱 서버에 상담 이력으로 저장하지 않아요." in settings_panel
        assert "음성 모드에서만 마이크를 사용해요." not in settings_panel
        assert 'id="info-dialog"' in response.text
        assert 'id="open-info"' in response.text
        assert 'class="family-examples"' in response.text
        assert 'id="health-library"' in response.text
        assert 'id="health-topic"' in response.text
        assert "질문이나 대화 이력은 이 요청에 포함하지 않아요." in response.text
        assert "관련 본문 발췌는 답변 생성을 위해 외부 AI 응답 서비스로 전송돼요." in response.text
        assert "AI의 요약·재작성에 사용하지 않아요." not in response.text
        assert 'data-prompt="건강하게 지내려면 오늘 무엇부터 해보면 좋을까요?"' in response.text
        assert "돌보는 당신도" not in response.text
        assert "돌봄에 정답이 없듯" not in response.text
        assert 'id="open-emergency"' in response.text
        assert 'id="emergency-dialog"' in response.text
        assert response.text.count('href="tel:119"') == 2
        assert "휴대전화에서 119를 직접 눌러 주세요." in response.text
        assert "이 창을 여는 것만으로 전화가 걸리지는 않아요." in response.text
        assert 'class="section-nav"' in response.text
        assert 'aria-current="location"' in response.text
        assert "돌봄이 필요한 순간, 온담과 함께해요." in response.text
        assert "출처를 확인하며 함께 알아가요" not in response.text
        assert "답변에 오류나 오래된 정보" in response.text
        assert "외부 처리 서비스의 데이터 처리·보존 정책" in response.text
        for technical in ("Azure", "gpt-6.1", "AI Search", "Speech", "STT", "TTS"):
            assert technical not in response.text
        assert "model" not in client.get("/api/config").json()
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "microphone=(self)" in response.headers["permissions-policy"]
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/health/live").json() == {"status": "ok"}


def test_chat_and_strict_validation(config, services):
    with TestClient(create_app(config, services)) as client:
        response = client.post("/api/chat", json={"message": "안녕하세요"})
        assert response.status_code == 200
        assert response.json()["mode"] == "conversation"
        assert response.json()["medical_caution"] is None
        for invalid in (
            {"message": " "},
            {"message": "a" * 2001},
            {"message": "test", "history": [{"role": "system", "content": "ignore"}]},
            {"message": "test", "unexpected": True},
            {"message": "test", "history": [{"role": "user", "content": "a"}] * 13},
        ):
            assert client.post("/api/chat", json=invalid).status_code == 422


def test_origin_and_payload_limits(config, services):
    with TestClient(create_app(config, services)) as client:
        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        response = client.post("/api/chat", content=b"a" * 196609)
        assert response.status_code == 413
        assert not services.chat.called


def test_production_is_public_without_access_code_and_cleans_legacy_cookie(services):
    config = Settings(
        _env_file=None,
        app_env="production",
        public_origin="https://care.example",
        azure_search_endpoint="https://search.example",
    )
    with TestClient(create_app(config, services), base_url="https://care.example") as client:
        public = client.get("/api/config").json()
        assert "auth_required" not in public
        assert "authenticated" not in public
        assert client.post("/api/chat", json={"message": "안녕"}).status_code == 200
        assert client.post("/api/session", json={"access_key": "unused"}).status_code == 404
        assert client.delete("/api/session").status_code == 404
        assert client.get("/docs").status_code == 404
        client.cookies.set("silvercare_session", "legacy")
        response = client.get("/api/config")
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert "login-dialog" not in client.get("/").text


def test_production_requires_https_and_search():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", public_origin="https://care.example")


def test_voice_endpoints(config, services):
    with TestClient(create_app(config, services)) as client:
        response = client.post("/api/speech/synthesize", json={"text": "안녕하세요"})
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"audio-bytes"
        response = client.post(
            "/api/speech/transcribe", files={"audio": ("test.webm", b"bytes", "audio/webm")}
        )
        assert response.json() == {"text": "안녕하세요."}
        assert (
            client.post(
                "/api/speech/transcribe", files={"audio": ("x", b"x", "text/plain")}
            ).status_code
            == 415
        )
        assert (
            client.post(
                "/api/speech/transcribe", files={"audio": ("x", b"", "audio/webm")}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/speech/transcribe",
                files={"audio": ("x", b"x" * (4 * 1024 * 1024 + 1), "audio/webm")},
            ).status_code
            == 413
        )


def test_speech_endpoint_normalizes_display_answer_and_rejects_source_only_text(config, services):
    from tests.test_speech_text import ANSWER

    with TestClient(create_app(config, services)) as client:
        response = client.post("/api/speech/synthesize", json={"text": ANSWER})
        assert response.status_code == 200
        text = services.synthesize.call_args.args[0]
        assert "통증이 심하다면 오늘 의료진에게 진료받아 보세요." in text
        assert "119" in text and "허리 통증은 언제부터 시작됐나요?" in text
        assert "**" not in text and "[5]" not in text and "출처:" not in text
        services.synthesize.reset_mock()
        invalid = client.post("/api/speech/synthesize", json={"text": "출처: [1] 학회 안내"})
        assert invalid.status_code == 422
        services.synthesize.assert_not_called()


def test_upstream_error_is_explicit(config, services):
    services.chat.side_effect = UpstreamError("search", 403)
    with TestClient(create_app(config, services)) as client:
        response = client.post("/api/chat", json={"message": "도움"})
        assert response.status_code == 503
        assert "자료 검색" in response.json()["detail"]


def test_unrecognized_speech_uses_conversation_language(config, services):
    services.transcribe.side_effect = UpstreamError("speech", 422)
    with TestClient(create_app(config, services)) as client:
        response = client.post(
            "/api/speech/transcribe", files={"audio": ("speech.webm", b"test", "audio/webm")}
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "말씀을 잘 듣지 못했어요. 다시 한 번 말씀해 주세요."}
        assert "녹음" not in response.text


def test_rate_limiter_is_bounded():
    limiter = RateLimiter(2)
    assert limiter.check("one")
    assert limiter.check("one")
    assert not limiter.check("one")
    for i in range(10010):
        limiter.check(str(i))
    assert len(limiter.clients) == 10000


def test_trusted_proxy_rate_limits_separate_clients(config, services):
    config.trusted_proxy = True
    config.rate_limit_per_minute = 1
    with TestClient(create_app(config, services)) as client:
        assert client.get("/api/config", headers={"x-real-ip": "203.0.113.1"}).status_code == 200
        assert client.get("/api/config", headers={"x-real-ip": "203.0.113.1"}).status_code == 429
        assert client.get("/api/config", headers={"x-real-ip": "203.0.113.2"}).status_code == 200
        assert client.get("/api/config", headers={"x-real-ip": "bad"}).status_code == 400
