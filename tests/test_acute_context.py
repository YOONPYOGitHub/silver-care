import httpx
import pytest

from silvercare.models import ChatRequest
from silvercare.safety import emergency_response
from silvercare.tracing import EvaluationTrace
from tests.test_azure import make_services

ACUTE = [
    "어머니가 조금 전부터 한쪽 눈이 전혀 안 보여요.",
    "기도폐쇄를 공부하던 중 아버지가 지금 숨을 쉴 수가 없다고 하세요.",
    "할머니가 떡이 목에 걸려서 숨을 쉴 수가 없어요. 말도 못해요.",
    "할아버지가 지금 밥 먹다가 목이 꽉 막혀 아무 소리도 못 내고 기침도 못 해요.",
    "음식이 기도를 막아서 어머니가 지금 숨이 안 들어간대요. 말도 못 하고 있어요.",
    "어머니가 빵을 먹고 목을 움켜쥐고 입술이 파래지는데 말을 못 해요.",
    "아버지가 떡을 먹다가 지금 숨을 못 쉬어요.",
    "어머니가 음식이 목에 걸려서 지금 호흡을 전혀 못 하고 있대요.",
    "10분 전부터 한쪽 눈이 갑자기 캄캄해져서 아무것도 보이지 않아요.",
    "아침까지 잘 보였는데 지금 오른쪽 눈이 갑자기 전혀 안 보여요. 아프지는 않아요.",
    "어머니가 방금 한쪽 눈앞이 깜깜해졌고 아직 아무것도 안 보인대요.",
    "방금 오른쪽 눈이 갑자기 하얘지면서 물체 모양도 전혀 구분할 수 없어요.",
    "아버지가 지금 숨을 쉴 수가 없고 한마디도 할 수가 없어요.",
    "어머니가 갑자기 헐떡거리며 단어 한 개도 말하지 못해요.",
    "할머니가 지금 호흡이 전혀 되지 않는다고 해요.",
    "아버지가 현재 숨이 너무 가빠 말을 끝까지 이어서 못 하고 있어요.",
    "어머니가 지금 숨을 못 쉬고 입술이 파래요.",
    "방금 갑자기 시력을 잃었어요.",
]

NONCURRENT = [
    "수업에서 숨을 쉴 수가 없다고 가정하고 대처법을 배우고 있어요. 실제 환자는 없습니다.",
    "교육 자료에서 한쪽 눈이 갑자기 전혀 안 보인다고 가정합니다. 현재 환자는 없어요.",
    "성인 기도폐쇄 응급처치 수업입니다. 말을 못 하고 숨을 못 쉬는 경우를 "
    "공부하는 중이며 지금 환자는 없어요.",
    "숨을 쉴 수 없는 상황을 공부하고 있어요. 실제 환자는 없습니다.",
    "한쪽 눈이 갑자기 전혀 안 보이는 경우를 배우고 싶어요. 지금 증상은 없습니다.",
    "목이 막혀 기침도 못 하는 상황을 가정해서 교육 자료를 만들고 있어요.",
    "태어날 때부터 왼쪽 눈이 안 보이고 평생 변화는 없어요. 생활 보조도구가 궁금해요.",
    "백내장 때문에 수년째 조금씩 눈이 침침한데 오늘 갑자기 달라진 점은 없어요.",
    "갑자기 한쪽 눈이 전혀 안 보이는 경우에는 어떻게 하는지 교육 중입니다. "
    "지금 증상이 있는 사람은 없어요.",
    "작년에 떡을 먹다 목에 걸렸지만 바로 뱉었고 지금은 아무 증상도 없어요.",
    "식사 때 가끔 사레가 들지만 지금은 편하게 숨 쉬며 대화하고 있어요.",
    "밥을 먹다 목이 따끔하지만 말도 잘하고 숨도 편하게 쉬어요.",
    "눈이 조금 침침해요. 평소와 같고 시력 검사 예약이 있어요.",
    "갑자기 왼쪽 눈이 뿌옇게 보여요.",
    "숨을 쉴 수 없는 건 아니고 지금 편하게 대화하고 있어요.",
    "지금 호흡이 전혀 되지 않는 건 아니에요.",
]


@pytest.mark.parametrize("message", ACUTE)
def test_clear_acute_paraphrases_request_immediate_help(message):
    response = emergency_response(message)
    assert response is not None
    assert response.mode == "emergency"
    assert "지금 즉시 119" in response.answer


@pytest.mark.parametrize("message", NONCURRENT)
def test_noncurrent_and_less_specific_symptoms_do_not_force_an_emergency(message):
    assert emergency_response(message) is None


@pytest.mark.parametrize("message", ACUTE)
async def test_clear_acute_danger_does_not_wait_for_search_or_generation(message):
    def handle(request):
        raise AssertionError(f"Acute help must not wait for {request.url.path}")

    service = make_services(handle)
    payload = ChatRequest(message=message)
    trace = EvaluationTrace.for_request(payload)
    try:
        response = await service.chat(payload, trace=trace)
        assert response.mode == "emergency"
        assert trace.data["route"] == "fixed_emergency"
        assert trace.data["generation_called"] is False
        assert trace.searches == []
    finally:
        await service.close()


async def test_classroom_context_reaches_normal_generation():
    def handle(request):
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "model": "test-model",
                    "output": [
                        {
                            "content": [
                                {"type": "output_text", "text": "현재 환자가 없는 교육 상황입니다."}
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"value": []})

    service = make_services(handle)
    try:
        response = await service.chat(ChatRequest(message=NONCURRENT[0]))
        assert response.mode != "emergency"
        assert "지금 즉시 119" not in response.answer
    finally:
        await service.close()
