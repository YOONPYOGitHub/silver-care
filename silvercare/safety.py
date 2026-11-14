import re

from silvercare.models import ChatResponse

NOTICE = (
    "온담은 AI로, 답변에 오류나 오래된 정보가 있을 수 있습니다. "
    "중요한 내용은 출처와 전문가를 통해 확인해 주세요."
)
MEDICAL_CAUTION = (
    "건강·약물·검사 정보는 일반적인 참고용이에요. "
    "답변만으로 질환을 판단하거나 약의 복용량·중단 여부, 검사 필요성을 결정하지 마세요. "
    "개인에게 맞는 판단은 의사·약사 등 의료전문가와 상의해 주세요."
)
MEDICAL_DISPLAY_NOTICE = (
    "AI 답변은 참고용이며 오류가 있을 수 있어요. "
    "답변만으로 진단·복약 변경·검사 여부를 결정하지 말고 의료진과 상의해 주세요."
)
MEDICAL_CONTEXT = re.compile(
    r"약물|의약품|복약|복용|투약|처방|검사|검진|치료|진단|증상|질환"
    r"|통증|당뇨|치매|혈압|혈당|부작용|용량|아스피린|인슐린|초음파"
    r"|(?<![가-힣])약(?:[이가을은의에]|\s|[?.!,]|$)"
    r"|(?<![A-Za-z])(?:MRI|CT)(?![A-Za-z])",
    re.IGNORECASE,
)
EMERGENCY = re.compile(
    r"숨[이가을]?\s*(?:안\s*쉬|안\s*쉬어|막혀|막히|못\s*쉬)"
    r"|호흡\s*(?:곤란|없|멈)"
    r"|의식[이가]?\s*(?:없|잃)"
    r"|(?:갑자기|갑작스).{0,15}(?:가슴|흉통|마비|말이|말을)"
    r"|(?:가슴|흉통).{0,15}(?:심하|심해|극심|쥐어짜)"
    r"|(?:죽고\s*싶|자살|목숨.{0,8}(?:끊|버리)|자해)"
    r"|(?:쓰러졌|쓰러져|깨워도.{0,8}(?:안|않))"
)


def medical_caution(message: str, answer: str) -> str | None:
    return MEDICAL_CAUTION if MEDICAL_CONTEXT.search(message + "\n" + answer) else None


def answer_display_notice(
    notice: str,
    caution: str | None,
    *,
    dataset_cited: bool,
    has_sources: bool,
) -> str:
    if not caution:
        return notice
    if caution != MEDICAL_CAUTION:
        return f"{notice} {caution}"
    parts = [MEDICAL_DISPLAY_NOTICE]
    if dataset_cited:
        parts.append(
            "일부 자료는 전문가 검토 전의 자동 정리본이며, "
            "AI Hub 링크는 원문이 아닌 데이터셋 안내입니다."
        )
    if not has_sources:
        parts.append("인용된 참고자료는 없습니다.")
    return " ".join(parts)


def emergency_response(message: str) -> ChatResponse | None:
    if not EMERGENCY.search(message):
        return None
    return ChatResponse(
        answer=(
            "지금은 채팅보다 사람의 도움이 먼저 필요할 수 있습니다.\n\n"
            "• 의식 저하, 호흡 곤란, 갑작스러운 가슴 통증·마비가 있다면 즉시 119에 전화해 주세요.\n"
            "• 스스로를 해칠 위험이 있다면 혼자 있지 말고 가까운 사람에게 알려 주세요. "
            "긴급하면 119 또는 112, 자살예방상담전화 109로 연락해 주세요.\n"
            "• 가능하면 보호자나 주변 사람에게 함께 있어 달라고 부탁해 주세요.\n\n"
            "이 서비스는 신고하거나 위치를 전송하지 않습니다. 직접 전화해 주세요."
        ),
        mode="emergency",
        notice=NOTICE,
    )
