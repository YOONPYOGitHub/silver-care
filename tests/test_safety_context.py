import httpx
import pytest

from silvercare.models import ChatRequest
from silvercare.safety import emergency_response
from silvercare.tracing import EvaluationTrace
from tests.test_azure import make_services


@pytest.mark.parametrize(
    "message",
    [
        "아버지는 태어날 때부터 왼팔에 힘이 없어요. 지금 새로 생긴 증상은 없고 "
        "평생 똑같은 상태입니다. 재활 상담은 어디서 하나요?",
        "선천적으로 오른쪽 팔에 힘이 없지만 변화는 없어요. 재활 상담이 궁금해요.",
        "평생 왼쪽 다리에 힘이 없었고 지금도 같은 상태예요. 보조기 상담을 받고 싶어요.",
        "예전부터 말이 어눌한 편인데 변화는 없어요. 발음 연습을 어디서 배우나요?",
        "수십 년 동안 발음이 어눌해요. 최근에 달라진 점은 없고 언어 재활이 궁금해요.",
        "몇 년째 밤에 자면서 숨이 안 쉬어지는 것 같다고 해요. "
        "지금은 편하게 숨 쉬며 대화 중이고 수면검사 상담이 궁금해요.",
        "수년째 계단을 오를 때만 호흡곤란이 있고 쉬면 괜찮아요. "
        "지금 증상은 없는데 외래에서 무엇을 물어보면 좋을까요?",
        "만성 호흡곤란은 예전과 같아요. 지금은 쉬며 편하게 대화하고 있고 진료 예약이 있어요.",
        "갑자기 한쪽 팔에 힘이 빠지고 말이 어눌해지면 어떻게 하나요? "
        "현재 증상은 없고 예방 교육이 궁금합니다.",
        "만약 숨이 막히고 의식을 잃으면 어느 번호에 전화해야 하나요?",
        "한쪽 팔에 힘이 빠지고 얼굴이 한쪽으로 처지는 경우 어떻게 해야 하나요?",
        "응급처치 교육 중입니다. 가슴이 갑자기 아프고 숨을 못 쉬면 무엇을 해야 하나요?",
        "머리를 부딪치고 여러 번 토하거나 깨우기 어려워지면 어떻게 해야 하나요? "
        "지금 다친 사람은 없습니다.",
        "호흡곤란이 생기고 의식이 없어지는 경우에 대해 공부하고 있어요.",
        "만약 지금 한쪽 팔에 힘이 빠지고 말이 어눌해지면 어떻게 하나요?",
        "10년째 말이 어눌한 편이고 변화는 없어요. 언어 재활이 궁금해요.",
        "발음이 어눌한 지는 20년째예요. 지금도 그대로고 상담받을 곳이 궁금해요.",
        "호흡곤란이 10년째 계단을 오를 때만 있어요. 지금은 쉬며 편하게 대화 중이에요.",
        "수십 년째 말이 어눌한 상태고 새로운 변화는 없어요.",
        "평생 말이 어눌한 편이고, 갑자기 생긴 증상은 전혀 없어요. 재활 상담이 궁금해요.",
        "호흡곤란이 10년째 같고 더 심해지지는 않았어요. 외래 질문을 정리하고 싶어요.",
        "발음이 어눌한 것은 30년 동안 똑같아요. 언어 재활 상담이 궁금해요.",
        "오른팔에 힘이 없는 상태가 태어날 때부터였어요. 최근 새 변화는 없어요.",
        "어릴 때부터 지금까지 말이 어눌한 편일 뿐이고 새로운 변화는 없어요.",
        "오래전부터 지금까지 한쪽 다리에 힘이 없는 상태예요. 재활 상담이 궁금해요.",
        "말이 어눌한 채로 수십 년째 살았어요. 새 증상 없이 언어 재활을 문의하고 싶어요.",
        "몇십 년째 말이 어눌한 편인데 지금도 변함없어요.",
    ],
)
def test_stable_and_linked_hypothetical_context_is_not_a_current_emergency(message):
    assert emergency_response(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "아버지는 평생 왼팔에 힘이 없었지만 방금 말이 어눌해지고 오른팔 힘도 빠졌어요.",
        "예전부터 말이 어눌했지만 오늘 갑자기 왼쪽 다리에 힘이 빠졌어요.",
        "만성 호흡곤란이 있었는데 지금은 갑자기 더 심해져 말을 못 하겠어요.",
        "평소 천식이 있지만 지금 숨을 못 쉬고 있어요.",
        "수면무호흡 검사를 기다리고 있는데 지금 깨워도 반응이 없어요.",
        "몇 년째 몸이 불편했지만 오늘 갑자기 가슴이 쥐어짜듯 아파요.",
        "10분 전에 오른팔에 힘이 빠지고 말이 어눌했지만 지금은 정상이에요.",
        "어제 잠깐 왼쪽 팔이 마비됐다가 풀렸어요. 지금은 괜찮아요.",
        "급한 경우를 공부하다가 아버지가 지금 갑자기 말이 어눌해졌어요.",
        "한쪽 팔에 힘이 빠지고 말이 어눌하면 어떻게 하나요? 지금 아버지가 그런 상태예요.",
        "숨을 못 쉬고 의식이 없으면 어떻게 하나요? 지금 어머니가 그런 상태입니다.",
        "머리를 부딪친 뒤 여러 번 토하고 깨우기 어려워요. 아침까지 기다릴까요?",
        "몇 년째 죽고 싶은 생각을 하고 있고 오늘도 자해 충동을 참기 어려워요.",
        "가슴 통증은 없고 호흡도 괜찮지만 지금 스스로를 해치고 싶어요.",
        "평소 건강한 아버지가 지금 말이 어눌해졌어요.",
        "아버지는 태어날 때부터 왼팔에 힘이 없고, 어머니는 지금 말이 어눌해졌어요.",
        "만성 폐질환으로 진료받는 아버지가 지금 숨을 못 쉬어요.",
        "지금호흡곤란이있고쉬면나아질까요?",
        "지금 왼쪽 팔에 힘이 빠졌고 말도 어눌해지면 119에 전화해야 하나요?",
        "수년째 호흡곤란이 있었는데 지금은 더 심해졌어요.",
        "선천적 심장질환으로 진료받는 아버지가 지금 숨을 못 쉬어요.",
        "평소 왼팔에 힘이 없었는데, 지금 오른팔에 힘이 빠지고 말이 어눌해졌어요.",
        "수년째 호흡곤란이 있었는데 오늘은 더 심해요.",
        "평소 명치가 불편하지만 오늘 갑자기 찬땀이 나고 숨이 가빠요.",
    ],
)
def test_chronic_or_educational_context_does_not_hide_new_actual_danger(message):
    response = emergency_response(message)
    assert response is not None
    assert response.mode == "emergency"
    assert "119" in response.answer or "109" in response.answer


@pytest.mark.parametrize(
    "question",
    [
        "태어날 때부터 왼팔에 힘이 없고 지금도 그대로예요. 재활 상담이 궁금해요.",
        "예전부터 말이 어눌하고 변화는 없어요. 언어 재활이 궁금해요.",
        "몇 년째 밤에 숨이 안 쉬는 것 같대요. 지금은 정상이고 수면검사가 궁금해요.",
        "갑자기 팔에 힘이 빠지고 말이 어눌해지면 어떻게 하나요? 현재 증상은 없어요.",
    ],
)
async def test_noncurrent_conditions_reach_normal_generation_not_fixed_alarm(question):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "model": "test-model",
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "상담 때 확인할 내용을 함께 정리할 수 있어요.",
                                }
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"value": []})

    service = make_services(handle)
    payload = ChatRequest(message=question)
    trace = EvaluationTrace.for_request(payload)
    try:
        response = await service.chat(payload, trace=trace)
        assert response.mode != "emergency"
        assert trace.data["route"] == "retrieval_generation"
        assert trace.data["generation_called"]
        assert "119" not in response.answer
        assert any(path.endswith("/responses") for path in calls)
    finally:
        await service.close()
