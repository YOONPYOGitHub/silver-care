import builtins
import socket

import pytest

from silvercare.models import ChatResponse
from silvercare.safety import NOTICE, emergency_response


@pytest.mark.parametrize(
    "message,context",
    [
        ("아버지가 숨을 못 쉬어요", "숨"),
        ("할아버지가 숨을 안 쉬고 계세요!", "숨"),
        ("숨이 막혀요. 도와주세요.", "숨"),
        ("호흡이 멈췄어요", "호흡"),
        ("호흡이 없어요", "호흡"),
        ("아버지가 숨을 쉬지 않아요.", "숨"),
        ("어머니가 호흡을 안 해요.", "호흡"),
        ("호흡 곤란이 있어요", "호흡"),
        ("심한 숨참 때문에 너무 힘들어요", "숨"),
        ("숨이 차서 말을 못 하겠어요", "숨"),
        ("어머니 의식이 없어요", "의식"),
        ("할머니가 의식을 잃었어요", "의식"),
        ("깨워도 아무 반응이 없어요", "의식"),
        ("아버지가 쓰러졌어요", "의식"),
        ("갑자기 가슴이 아파요", "가슴"),
        ("갑작스런 흉통이 생겼어요.", "가슴"),
        ("가슴이 쥐어짜듯 아파요", "가슴"),
        ("흉통이 극심해요", "가슴"),
        ("극심한 가슴 통증이 있어요.", "가슴"),
        ("가슴이 심하게 아파요.", "가슴"),
        ("가슴이 갑자기 아프기 시작했어요", "가슴"),
        ("오른쪽 팔에 힘이 빠지고 말이 어눌해졌어요", "한쪽"),
        ("갑자기 왼쪽 다리에 힘이 안 들어가요", "한쪽"),
        ("갑작스럽게 말이 어눌해지고 있어요", "말"),
        ("갑자기 몸이 마비된 것 같아요", "한쪽"),
        ("갑자기 얼굴 한쪽이 처졌어요", "한쪽"),
        (
            "할머니가 넘어진 뒤 머리를 부딪쳤어요. 처음엔 괜찮다더니 "
            "지금은 여러 번 토하고 자꾸 졸아서 깨우기 어려워요. 아침까지 지켜볼까요?",
            "머리",
        ),
        ("문틀에 이마를 찧은 뒤 구토가 반복돼요.", "머리"),
        ("뒤통수를 바닥에 부딪치고 나서 계속 토해요.", "머리"),
        ("머리를 다쳤는데 깨우기가 너무 힘들어요.", "머리"),
        ("계속 토하고 있어요. 오늘 아침 머리를 세게 부딪쳤거든요.", "머리"),
    ],
)
def test_medical_red_flags_have_context_without_unrelated_crisis_language(message, context):
    response = emergency_response(message)
    assert isinstance(response, ChatResponse)
    assert response.mode == "emergency"
    assert context in response.answer
    assert "119" in response.answer
    assert "109" not in response.answer
    assert "112" not in response.answer
    assert not any(word in response.answer for word in ("자살", "자해", "스스로를 해칠"))
    assert response.notice == NOTICE
    assert response.medical_caution is None
    assert response.sources == []
    assert response.health_topics == []


@pytest.mark.parametrize(
    "message",
    [
        "몇 분 전 왼팔에 힘이 빠지고 말이 어눌했는데 지금은 정상이에요.",
        "아까 갑자기 말이 안 나왔다가 괜찮아졌어요. 내일 가도 될까요?",
        "잠깐 오른쪽 다리에 힘이 빠졌어요. 이제 증상은 없는데 괜찮나요?",
        "어제 갑자기 한쪽 팔이 마비됐다가 풀렸어요.",
        "방금 말이 어눌해졌다가 돌아왔어요. 지금은 아무렇지도 않아요.",
        "왼쪽 팔에 힘이 없었다가 지금은 괜찮아졌습니다.",
        "몇 년 전에도 아팠지만 지금 갑자기 말이 어눌해졌어요.",
        "평소 피곤하지만 오늘 갑자기 왼쪽 팔에 힘이 빠졌어요.",
        "심한 숨참은 없지만 방금 오른쪽 팔이 마비됐어요.",
    ],
)
def test_transient_or_new_neurological_red_flags_are_not_cleared_by_current_recovery(message):
    response = emergency_response(message)
    assert response is not None
    assert "119" in response.answer
    assert "좋아졌더라도" in response.answer
    assert "109" not in response.answer


@pytest.mark.parametrize(
    "message",
    [
        "",
        "   ",
        "안녕하세요. 오늘 날씨가 좋네요.",
        "오늘 좀 피곤하고 졸려요.",
        "잠을 못 자서 몸에 힘이 없어요.",
        "몇 달째 무릎이 조금 아파요.",
        "오른팔이 몇 달째 조금 저려요.",
        "평소 왼쪽 팔에 힘이 없어요. 재활 상담이 궁금해요.",
        "늘 말이 어눌해요. 언어 재활이 궁금해요.",
        "가슴이 살짝 뻐근한 건 오래됐어요.",
        "갑자기 가슴이 간지러워요.",
        "갑자기 할 말이 생각났어요.",
        "갑자기 가슴이 벅차서 기뻐요.",
        "심한 숨참이나 가슴 통증은 없어요",
        "심한 숨참, 가슴 통증은 전혀 없어요.",
        "숨이 막히지는 않아요",
        "숨을 못 쉬는 건 아니에요.",
        "숨을 쉬지 못하는 건 아니에요.",
        "호흡 곤란은 없어요.",
        "호흡이 멈춘 적은 없어요.",
        "의식이 없는 건 아니에요.",
        "갑자기 가슴이 아프진 않아요.",
        "가슴 통증이 심하지 않아요.",
        "갑자기 말이 어눌해진 적은 없어요.",
        "왼쪽 팔에 힘이 빠지지는 않았어요.",
        "왼팔에 힘이 빠지는 건 아니에요.",
        "자살 생각은 없어요",
        "죽고 싶지 않아요",
        "죽고 싶다는 생각은 없어요.",
        "자해할 생각은 전혀 없어요.",
        "자해를 안 해요.",
        "스스로를 해치고 싶지 않아요.",
        "스스로를 해칠 생각은 없어요.",
        "목숨을 끊고 싶은 건 아니에요.",
        "자살 예방 교육 자료를 찾고 있어요.",
        "자살에 관한 기사를 읽었어요.",
        "자해를 다룬 책 내용을 설명해 주세요.",
        "자해라는 단어의 뜻이 궁금해요.",
        "자살예방상담전화 번호가 뭔가요?",
        "숨이 막히면 어떻게 신고해야 하나요?",
        "의식이 없는 경우 어떤 번호로 연락하나요?",
        "갑자기 가슴이 아프면 어떻게 해야 하나요?",
        "자살 생각이 들면 어디에 상담하나요?",
        "몇 년 전 의식을 잃었지만 지금은 건강합니다.",
        "몇 달 전 말이 어눌했던 적이 있어요. 지금은 건강해요.",
        "작년에 갑자기 가슴이 아팠지만 이후 괜찮아요.",
        "어릴 때 머리를 다치고 여러 번 토한 적이 있어요. 지금은 건강해요.",
        "예전에 자살 생각을 했지만 지금은 전혀 없어요.",
        "머리를 부딪쳤지만 구토는 없고 정신은 또렷해요.",
        "머리를 부딪친 것은 아니고 여러 번 토했어요.",
        "머리를 부딪친 뒤 한 번 토했지만 다른 증상은 없어요.",
        "머리를 살짝 부딪쳤어요. 잠을 못 자서 평소처럼 졸려요.",
        "머리를 다쳤지만 깨우기가 어렵지는 않아요.",
        "머리를 찧었지만 깨우기가 힘들지는 않아요.",
        "구토를 반복하지만 머리를 다친 적은 없어요.",
        "계속 토해요.",
    ],
)
def test_nonemergency_negated_educational_remote_history_and_mild_messages_return_none(message):
    assert emergency_response(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "죽고 싶어요",
        "자살하고 싶어요",
        "요즘 자해 충동을 참기 어려워요.",
        "제 목숨을 끊고 싶어요.",
        "혼자 있으면 스스로를 해칠 것 같아요.",
        "살고 싶지 않아요.",
        "자살 생각이 없지는 않아요.",
        "예전엔 괜찮았지만 오늘은 죽고 싶어요.",
        "어머니가 목숨을 버리겠다고 해요.",
        "가슴 통증은 없어요. 하지만 자해하고 싶어요.",
    ],
)
def test_self_harm_routing_has_crisis_support_not_unrelated_medical_symptoms(message):
    response = emergency_response(message)
    assert response is not None
    assert response.mode == "emergency"
    for number in ("109", "112", "119"):
        assert number in response.answer
    assert not any(word in response.answer for word in ("가슴", "마비", "구토", "호흡 곤란"))


@pytest.mark.parametrize(
    "message",
    [
        "자살 생각은 없어요. 그런데 숨을 못 쉬어요.",
        "죽고 싶지 않아요. 갑자기 가슴이 아파요.",
        "호흡 곤란은 없지만 가슴이 쥐어짜듯 아파요.",
        "평소 피곤한데 오늘 갑자기 말이 어눌해졌어요.",
        "숨이 막히면 어떻게 하죠? 지금은 어머니 의식이 없어요.",
        "작년에 가슴이 아팠어요. 지금은 아버지가 숨을 안 쉬어요.",
        "머리를 부딪친 적은 없지만 갑자기 오른쪽 팔이 마비됐어요.",
        "가슴이 갑자기 아프고 구토는 없어요.",
    ],
)
def test_denials_and_other_context_do_not_mask_a_separate_actual_medical_red_flag(message):
    response = emergency_response(message)
    assert response is not None
    assert "119" in response.answer
    assert "109" not in response.answer


def test_actual_medical_and_self_harm_danger_can_both_be_acknowledged():
    response = emergency_response("갑자기 가슴이 아프고 자살 생각도 들어요.")
    assert response is not None
    assert "가슴" in response.answer and "109" in response.answer


@pytest.mark.parametrize(
    "message",
    [
        "어머니가 명치가 불편하다며 식은땀을 흘리고 숨이 차다고 해요. 심한 가슴 통증은 없대요.",
        "아버지가 윗배 통증과 함께 찬 땀이 나고 숨이 가빠요. 조금 쉬어도 될까요?",
        "가슴이 답답해요. 식은땀도 나요. 숨도 차요.",
        "식은땀을 흘려요. 어깨가 뻐근하고 숨이 차요.",
        "명치가 불편하고 숨이 찬데 식은땀도 납니다.",
        "평소 가벼운 윗배 불편감이 있었지만 오늘은 갑자기 식은땀이 나고 숨이 가빠졌어요.",
        "가슴이 심하게 아프지는 않아요. 다만 명치가 답답하고 식은땀이 나면서 숨이 차요.",
        "몇 년 전에도 아팠지만 지금은 처음 느끼는 명치 불편감에 찬땀이 나고 숨이 가쁩니다.",
        "평소 가슴이 답답해요. 오늘 식은땀과 숨참이 새로 나타났어요.",
    ],
)
def test_upper_body_discomfort_with_cold_sweat_and_breathlessness_escalates(message):
    response = emergency_response(message)
    assert response is not None
    assert response.mode == "emergency"
    for text in ("불편", "식은땀", "숨", "119", "기다리지", "주변", "직접 전화"):
        assert text in response.answer
    assert "원인은 채팅으로 판단할 수 없습니다" in response.answer
    assert not any(word in response.answer for word in ("109", "112", "자살", "심근경색", "복용"))
    assert "갑작스럽거나 심한 가슴 통증" not in response.answer


@pytest.mark.parametrize(
    "message",
    [
        "명치가 불편해요.",
        "식은땀이 나고 숨이 조금 차요.",
        "윗배가 불편하고 식은땀이 나요.",
        "가슴이 답답하고 숨이 차요.",
        "명치가 불편하고 식은땀이 나지만 숨은 차지 않아요.",
        "윗배가 불편하고 식은땀이 나지만 숨이 가쁘지는 않아요.",
        "가슴이 답답하고 식은땀이 나지만 숨참은 없어요.",
        "명치가 불편하고 식은땀이 나지만 숨이 찬 건 아니에요.",
        "식은땀은 없고 윗배가 불편하면서 숨이 차요.",
        "명치가 불편하고 숨이 차지만 식은땀이 나지 않아요.",
        "명치가 불편하고 숨이 차지만 식은땀은 안 나요.",
        "명치가 불편하고 숨이 차지만 식은땀을 흘리지는 않아요.",
        "명치가 불편하고 숨이 차지만 식은땀을 안 흘려요.",
        "명치가 불편하고 숨이 차지만 식은땀이 전혀 나지 않아요.",
        "식은땀이 나고 숨이 차지만 명치가 불편하지는 않아요.",
        "윗배 통증은 없고 식은땀이 나면서 숨이 차요.",
        "명치가 불편하고 식은땀이 나면서 숨이 차면 어디에 연락해야 하나요?",
        "윗배 통증과 식은땀, 숨참이 있는 경우를 설명한 자료가 있나요?",
        "몇 년 전 명치가 불편하고 식은땀이 나면서 숨이 찼어요. 지금은 건강합니다.",
        "명치 불편감과 식은땀, 숨참이 있었던 건 작년이에요. 지금은 증상 없어요.",
        "몇 년째 명치가 조금 불편하고 식은땀이 나고 숨이 차지만 변함없어요.",
        "숨이 차고 찬땀이 나는 건 평소와 똑같고 윗배도 늘 조금 불편해요.",
        "평소 가슴 답답함과 식은땀, 숨참이 늘 똑같아요.",
        "오늘도 몇 달째 변화 없이 명치가 조금 불편하고 식은땀이 나고 숨이 차요.",
        "몇 년째 명치가 불편하고 식은땀이 나고 숨이 찬 상태로 처음부터 그대로예요.",
        "평소 명치가 불편하고 식은땀이 나고 숨이 차지만 심해지지는 않았어요.",
        "평소 명치가 불편하고 식은땀이 나고 숨이 차지만 새로 생긴 증상은 없어요.",
        "몇 년 전 명치가 불편했어요. 오늘 식은땀이 나고 숨이 조금 차요.",
        "명치가 불편해요. "
        + "다른 이야기를 길게 하고 있어요. " * 25
        + "식은땀이 나고 숨이 조금 차요.",
    ],
)
def test_upper_body_warning_combination_requires_all_current_nonnegated_signs(message):
    assert emergency_response(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "숨이 안 쉬어져요.",
        "의식이 없어요.",
        "갑자기 가슴이 아파요.",
        "갑자기 한쪽 팔에 힘이 빠졌어요.",
        "머리를 부딪친 뒤 계속 토해요.",
        "윗배가 불편하고 식은땀이 나면서 숨이 차요.",
        "죽고 싶어요.",
    ],
)
def test_emergency_actions_are_bounded_and_do_not_diagnose_or_prescribe(message):
    response = emergency_response(message)
    assert response is not None
    for text in ("채팅", "기다리지", "주변", "함께", "신고하거나 위치를 전송하지", "직접 전화"):
        assert text in response.answer
    assert not any(
        word in response.answer
        for word in (
            "뇌졸중",
            "뇌출혈",
            "심근경색",
            "진단입니다",
            "아스피린",
            "복용",
            "마사지",
            "음식을",
            "물을",
            "압박",
            "눕히",
        )
    )


@pytest.mark.parametrize(
    "message",
    [
        "숨이 안 쉬어져요.",
        "의식이 없어요.",
        "갑자기 가슴이 아파요.",
        "아까 왼쪽 팔에 힘이 빠졌다가 돌아왔어요.",
        "머리를 부딪치고 구토가 반복돼요.",
        "명치가 불편하고 찬땀이 나면서 숨이 차요.",
        "죽고 싶어요.",
        "오늘은 기분이 좋아요.",
    ],
)
def test_routing_needs_no_model_credentials_imports_or_network(monkeypatch, message):
    original_import = builtins.__import__

    def offline_import(name, *args, **kwargs):
        if name.startswith(("azure", "openai", "httpx", "silvercare.azure")):
            pytest.fail(f"Safety routing must not import model/network dependencies: {name}")
        return original_import(name, *args, **kwargs)

    def blocked_network(*args, **kwargs):
        pytest.fail("Safety routing must not use a network")

    for key in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_SEARCH_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(builtins, "__import__", offline_import)
    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    monkeypatch.setattr(socket, "create_connection", blocked_network)

    response = emergency_response(message)
    assert (response is None) == (message == "오늘은 기분이 좋아요.")
