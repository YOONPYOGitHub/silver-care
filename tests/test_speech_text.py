import pytest
from pydantic import ValidationError

from silvercare.models import SpeechRequest

ANSWER = (
    "허리가 많이 아프시군요. **통증이 심하다면 오늘 의료진에게 진료받아 보세요.** "
    "증상만으로는 원인을 알기 어렵습니다.\n\n"
    "다리에 힘이 갑자기 빠지거나, 엉덩이 사이 감각이 둔해지거나, "
    "소변·대변을 조절하기가 새로 어려워졌다면 응급 상황일 수 있습니다. "
    "즉시 119에 연락해 도움을 요청하세요.[5]\n\n"
    "허리 통증은 언제부터 시작됐나요?\n\n"
    "출처: [5] 신경외과학회, 마미 증후군 자료"
)


def test_spoken_answer_omits_display_markup_and_sources_but_keeps_all_advice():
    spoken = SpeechRequest(text=ANSWER).text
    expected = ANSWER.replace("**", "").replace("[5]", "").split("\n\n출처:", 1)[0]
    assert spoken == expected
    assert "즉시 119에 연락해 도움을 요청하세요." in spoken
    assert spoken.endswith("허리 통증은 언제부터 시작됐나요?")
    assert "신경외과학회" not in spoken


@pytest.mark.parametrize(
    "answer,expected",
    [
        (
            "### **안내**\n- **약은 임의로 끊지 마세요.** [1, 2]\n"
            "2. 5mg을 1~2회라는 기록은 의료진과 확인하세요.",
            "안내\n약은 임의로 끊지 마세요.\n5mg을 1~2회라는 기록은 의료진과 확인하세요.",
        ),
        ("[공식 안내](https://example.test/guide)를 확인하세요.[2][3]", "공식 안내를 확인하세요."),
        (
            "**출처**\n[1] 학회 안내\n[2] 기관 설명\n\n"
            "갑자기 다리에 힘이 빠지면 즉시 도움을 요청하세요.",
            "갑자기 다리에 힘이 빠지면 즉시 도움을 요청하세요.",
        ),
        (
            "질문입니다.\n\n## 참고자료\n- [1] 학회 자료\n- [기관](https://example.test/)\n",
            "질문입니다.",
        ),
        (
            "*가벼운 활동*과 _휴식_을 번갈아 하세요. ~~반드시~~ `기록`해 주세요.",
            "가벼운 활동과 휴식을 번갈아 하세요. 기록해 주세요.",
        ),
        (
            "> 물은 하루 1.5~2 L로 제한하라는 말을 들었다면 담당 의료진과 확인하세요.",
            "물은 하루 1.5~2 L로 제한하라는 말을 들었다면 담당 의료진과 확인하세요.",
        ),
        (
            "출처를 확인하는 것도 중요해요. 참고자료가 없으면 단정하지 마세요.",
            "출처를 확인하는 것도 중요해요. 참고자료가 없으면 단정하지 마세요.",
        ),
        ("출처: [1] 공식 기관\n\n**약을 두 배로 드시지 마세요.**", "약을 두 배로 드시지 마세요."),
        ("도움은 https://example.test/path 에서 확인하세요.", "도움은 원문 링크에서 확인하세요."),
        (
            "본문 안내입니다.\n\n### 출처\n\n- [1] 학회 자료\n\n"
            "### 주의\n- 약을 임의로 끊지 마세요.",
            "본문 안내입니다.\n\n주의\n약을 임의로 끊지 마세요.",
        ),
        (
            "참고문헌:\n1. 기관 자료\n2. 학회 설명\n\n- 119에 직접 전화하세요.",
            "119에 직접 전화하세요.",
        ),
        (
            "[공식 설명](https://example.test/guide_(topic))을 확인하세요.",
            "공식 설명을 확인하세요.",
        ),
    ],
)
def test_spoken_text_normalization_preserves_meaning(answer, expected):
    assert SpeechRequest(text=answer).text == expected


@pytest.mark.parametrize("text", ["   ", "** **", "[1]", "출처: [1] 학회 설명"])
def test_speech_with_no_spoken_body_is_rejected(text):
    with pytest.raises(ValidationError):
        SpeechRequest(text=text)


def test_spoken_text_is_idempotent_for_endpoint_and_direct_service_call():
    first = SpeechRequest(text=ANSWER).text
    assert SpeechRequest(text=first).text == first
