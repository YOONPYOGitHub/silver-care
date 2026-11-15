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
_SUDDEN = r"(?:갑자기|갑작스(?:러운|런|럽게|레))"
_BREATHING = re.compile(
    r"숨[이가을은는도]?\s*(?:안\s*쉬|못\s*쉬|쉬지\s*(?:못|않)|막혀|막히"
    r"|쉴\s*수[가는도]?\s*없|안\s*들어(?:가|간))"
    r"|호흡[이가은는]?\s*(?:곤란|없|멈|정지)"
    r"|호흡[을이]?\s*안\s*(?:해|하)"
    r"|호흡[을이가]?\s*(?:전혀\s*)?(?:못\s*하|되지\s*않|되지\s*못|안\s*되)"
    r"|(?:심한|극심한)\s*숨참"
    r"|(?:숨[이가을]?\s*(?:너무\s*)?(?:차서|가빠서|가빠)|헐떡거리)"
    r"[^.!?\n;]{0,28}(?:말[^.!?\n;]{0,12}못|말하기\s*어려|단어[^.!?\n;]{0,12}못)"
)
_CHOKING = re.compile(
    r"(?:(?:떡|밥|빵|음식)[^.!?\n;]{0,18}(?:먹|목|기도)"
    r"|(?:목|기도)[^.!?\n;]{0,10}(?:막|움켜쥐))"
    r"[^.!?\n;]{0,65}(?:소리[도를을]?\s*못|말[을도]?\s*못|기침[을도]?\s*못"
    r"|입술[^.!?\n;]{0,10}파래)"
)
_VISION_LOSS = re.compile(
    r"(?:눈(?:앞)?|시력)[^.!?\n;]{0,40}"
    r"(?:전혀\s*(?:안\s*보|보이지)|아무것도\s*(?:안\s*보|보이지)"
    r"|(?:깜깜|캄캄)해|물체[^.!?\n;]{0,20}구분[^.!?\n;]{0,12}(?:못|없))"
    r"|시력[을이가]?\s*(?:잃|상실)"
)
_VISION_ONSET = re.compile(rf"{_SUDDEN}|방금|조금\s*전|아까|(?:[0-9]+|몇)\s*분\s*전")
_CONSCIOUSNESS = re.compile(
    r"의식[이가을은는]?\s*(?:없|잃)"
    r"|(?:쓰러졌|쓰러져|쓰러지셨)"
    r"|깨워도[^.!?\n;]{0,12}(?:안|않|반응[이가]?\s*없)"
)
_CHEST = re.compile(
    rf"{_SUDDEN}[^.!?\n;]{{0,16}}"
    r"(?:가슴[이에]?\s*(?:아프|아파|통증|쥐어짜|짓누르)|흉통)"
    rf"|가슴[이에]?\s*{_SUDDEN}\s*(?:아프|아파|통증)"
    r"|(?:가슴\s*통증|흉통)[이가은는]?\s*(?:너무\s*|매우\s*)?(?:심하|심해|극심)"
    r"|가슴[이에]?\s*(?:쥐어짜|짓누르|심하게\s*아프|심하게\s*아파|극심한\s*통증)"
    r"|(?:심한|극심한|쥐어짜는|짓누르는)\s*(?:가슴\s*통증|흉통)"
)
_UPPER_BODY_DISCOMFORT = re.compile(
    r"(?:명치|윗배|위쪽\s*배|상복부|가슴|흉부|어깨|턱|등)"
    r"(?:\s*쪽)?[이가은는에도]?\s*"
    r"(?:(?:약간|조금|좀|늘)\s*)?(?:불편감|불편|답답함|답답|뻐근|아프|아파|통증)"
)
_DIAPHORESIS = re.compile(
    r"(?:식은땀|찬\s*땀)(?:[이을은는도]?\s*(?:전혀\s*)?"
    r"(?:흘리|흘려|흘러|흐르|났|나|난|납))?"
)
_BREATHLESSNESS = re.compile(r"숨[이가은는도]?\s*(?:차|찬|찼|가쁘|가빠|가쁩)|숨참")
_NEUROLOGICAL = re.compile(
    r"(?:한쪽|왼쪽|오른쪽|좌측|우측|왼팔|오른팔|왼다리|오른다리)"
    r"[^.!?\n;]{0,18}(?:힘[이가을]?\s*(?:빠지|빠졌|빠져|빠진|없|안\s*들어)"
    r"|마비|처졌|처지|쳐졌)"
    r"|(?:얼굴|입꼬리)[^.!?\n;]{0,8}(?:한쪽|왼쪽|오른쪽)"
    r"[^.!?\n;]{0,8}(?:처졌|처지|쳐졌)"
    r"|(?:말[이을]?\s*(?:잘\s*)?(?:어눌|안\s*나)|발음[이은]?\s*어눌)"
    rf"|{_SUDDEN}[^.!?\n;]{{0,16}}마비"
)
_HEAD_IMPACT = re.compile(
    r"(?:머리|뒤통수|이마|정수리|후두부)[^.!?\n;]{0,14}"
    r"(?:부딪|찧|다치|다쳤|다친|충격|맞았|박았)"
)
_HEAD_WARNING = re.compile(
    r"(?:여러\s*번|반복(?:해서|하여|적으로)?|계속|거듭|자꾸)"
    r"\s*(?:토하|토했|토해|구토)"
    r"|(?:구토|토하|토했|토해)[^.!?\n;]{0,8}(?:반복|여러\s*번|계속)"
    r"|깨우기[가도]?\s*(?:너무\s*|몹시\s*)?(?:어렵|어려|힘들)"
)
_SELF_HARM = re.compile(
    r"죽고\s*싶|살고\s*싶지\s*않"
    r"|(?:자살|자해)[을를이가도]?\s*"
    r"(?:(?:하고\s*싶|할\s*생각|생각|충동|계획|하려|하겠|할래|했|해요|하고\s*있))?"
    r"|목숨[^.!?\n;]{0,8}(?:끊고\s*싶|끊|버리)"
    r"|스스로[를가]?\s*[^.!?\n;]{0,8}(?:해치고\s*싶|해치|해칠)"
)

_CLAUSE_BREAK = re.compile(r"[.!?;\n]|(?:지만|는데|더니|고)(?:\s+|\s*[,，]\s*)")
_CONDITION_BREAK = re.compile(r"[.!?;\n]|(?:지만|는데|더니)(?:\s+|\s*[,，]\s*)")
_SENTENCE_BREAK = re.compile(r"[.!?;\n]")
_DENIED_SUFFIX = re.compile(
    r"^(?:(?:이나|나|과|와|또는|,)\s*(?:심한\s*)?"
    r"(?:숨참|가슴\s*통증|흉통|호흡\s*곤란)\s*){0,3}(?:"
    r"(?:하|히|리|치|으)?(?:지(?:는|도)?|진)\s*않"
    r"|(?:[은는이가을를도]|다는|하려는)?\s*"
    r"(?:(?:증상|생각|충동|계획|의도)[은는이가도]?\s*)?"
    r"(?:(?:전혀|아예)\s*)?(?:없|아니)"
    r"|[은는을를]?\s*안\s*(?:해|하)"
    r"|[가-힣]{0,4}\s*(?:건|것[은이도]?|게)\s*아니"
    r"|[가-힣]{0,4}\s*적[은이도]?\s*(?:한\s*번도\s*)?없)"
)
_DOUBLE_NEGATIVE = re.compile(r"없(?:지(?:는|도)?|진)\s*않|않(?:은|는)\s*(?:건|것은)\s*아니")
_HYPOTHETICAL_SUFFIX = re.compile(
    r"^[가-힣]{0,5}(?:면|다면)(?:\s|$)"
    r"|^[가-힣]{0,4}\s*(?:경우|때)(?:[는엔도에]|\s|$)"
    r"|^[이가은는]?\s*(?:있|들|생기)[가-힣]{0,4}면(?:\s|$)"
    r"|^[이가은는]?\s*(?:있는\s*경우|있을\s*때|생긴다면)"
    r"|^[가-힣]{0,4}\s*(?:경우|상황|증상)[을를]\s*"
    r"(?:공부|배우|학습|설명|가정|교육|연습)"
    r"|^[가-힣]{0,5}(?:다고|라고)\s*(?:가정|설명|배우|학습|교육|연습)"
)
_REFERENCE_SUFFIX = re.compile(
    r"^\s*(?:(?:에\s*관한|[을를]?\s*다룬|관련)\s*)?"
    r"(?:예방|기사|보도|통계|교육|자료|책|뉴스)"
    r"|^\s*(?:라는\s*단어|이라는\s*말|의\s*뜻)"
)
_REMOTE_HISTORY = re.compile(
    r"어릴\s*때|어렸을\s*때|어린\s*시절|수년\s*전|몇\s*(?:달|개월|년)\s*전"
    r"|작년|재작년|예전에|과거에|오래\s*전|[0-9]+\s*년\s*전"
)
_CURRENT = re.compile(r"지금|오늘|방금|아까|조금\s*전|이번|최근")
_BASELINE_DURATION = re.compile(
    r"선천(?:적(?:으로)?)?|태어날\s*때부터|태어나서부터|평생|예전부터"
    r"|어릴\s*때부터|어렸을\s*때부터|어린\s*시절부터|오래\s*전부터"
    r"|(?:[0-9]+|몇십|수십|몇|수)\s*(?:달|개월|년)\s*(?:째|동안|간)|오랫동안"
)
_USUAL_PREFIX = re.compile(
    r"(?:평소(?:에도|부터)?|원래|늘|항상|만성(?:적으로)?)\s*(?:(?:조금|약간|다소)\s*)?$"
)
_BASELINE_ADVERBS = re.compile(
    r"(?:(?:동안|부터|는|도|밤에|낮에|자면서|잠잘때|수면중에?|가끔|종종|계속"
    r"|계단을오를때만|운동할때만|약간|조금|지금까지|현재까지|오늘까지)\s*)*"
)
_UNCHANGED = re.compile(r"변함없|변화(?:가|는)?\s*없|평소와\s*같|예전과\s*(?:같|똑같)|그대로")
_BASELINE_SUFFIX = re.compile(
    r"\s*[이가은는도]?\s*(?:(?:한|해진|했던|있은|있던|느낀|는|은|었던)\s*"
    r"(?:지(?:는|가)?|것[은이가도]?|건|상태[가는]?|채로)\s*)?"
)
_CURRENT_EVENT = re.compile(r"(?:지금|현재|방금|오늘|아까)(?!까지|도)")
_CURRENT_REPORT = re.compile(
    r"(?:지금|현재|방금)[^.!?\n;]{0,20}(?:그런|이런|해당|같은)\s*(?:증상|상태)"
    r"[이가]?\s*(?:예요|이에요|입니다|있|생겼|발생)"
)
_HYPOTHETICAL_FRAME = re.compile(r"만약|가정|예방|교육|공부|훈련|수업|학습|연습")
_COMPLETED_EVENT = re.compile(r"졌|했|됐|었|았|어요|습니다|입니다")
_SWEAT_DENIED_SUFFIX = re.compile(
    r"^[이가을은는도]?\s*(?:전혀\s*)?안\s*(?:나(?:요|는|서|고|도|$)|흘려|흘리)"
)
_STABLE_COMBINATION = re.compile(
    rf"{_BASELINE_DURATION.pattern}|평소|원래|늘\s|항상|만성|{_UNCHANGED.pattern}|오래됐"
)
_NEW_OR_WORSE = re.compile(
    r"새로\s*(?:생기|생긴|나타나|나타났)"
    r"|처음\s*(?:생긴|생기|나타난|나타나)"
    r"|(?:더\s*)?(?:심해지|심해졌)|더\s*(?:심하|심해)"
    r"|악화되|악화됐|가빠지|가빠졌|달라지|달라졌"
    r"|평소와\s*달리|전과\s*달리"
)

_MEDICAL_ROUTES = (
    (
        _CHOKING,
        "음식이나 이물로 목이 막히고 말하거나 효과적으로 기침하기 어려운 상황은 "
        "즉시 도움이 필요합니다.",
    ),
    (
        _BREATHING,
        "숨을 제대로 쉬기 어렵거나 호흡이 없는 상황은 긴급한 확인이 필요할 수 있습니다.",
    ),
    (
        _CONSCIOUSNESS,
        "의식을 잃거나 쓰러지는 상황, 깨워도 반응이 없는 상황은 긴급한 확인이 필요할 수 있습니다.",
    ),
    (
        _NEUROLOGICAL,
        "한쪽에 힘이 빠지거나 말이 달라지는 증상은 긴급한 확인이 필요할 수 있습니다. "
        "잠깐 나타났다가 좋아졌더라도 기다리지 마세요.",
    ),
    (
        _CHEST,
        "갑작스럽거나 심한 가슴 통증은 긴급한 확인이 필요할 수 있습니다.",
    ),
)

_MEDICAL_PATTERNS = tuple(pattern for pattern, _ in _MEDICAL_ROUTES) + (
    _VISION_LOSS,
    _HEAD_IMPACT,
    _HEAD_WARNING,
    _UPPER_BODY_DISCOMFORT,
    _DIAPHORESIS,
    _BREATHLESSNESS,
)


def _context_bounds(
    pattern: re.Pattern[str], message: str, match: re.Match[str]
) -> tuple[int, int]:
    boundaries = list(pattern.finditer(message))
    start = max((part.end() for part in boundaries if part.end() <= match.start()), default=0)
    end = min(
        (part.start() for part in boundaries if part.start() >= match.end()),
        default=len(message),
    )
    return start, end


def _denied(suffix: str) -> bool:
    return bool(_DENIED_SUFFIX.search(suffix) and not _DOUBLE_NEGATIVE.search(suffix))


def _current_state_reported(message: str) -> bool:
    return any(not _denied(message[match.end() :]) for match in _CURRENT_REPORT.finditer(message))


def _conditional_suffix(suffix: str) -> bool:
    match = _HYPOTHETICAL_SUFFIX.search(suffix.lstrip())
    return bool(match and not re.search(r"(?<![다라])고|지만|는데|더니", match.group()))


def _linked_condition(pattern: re.Pattern[str], message: str, match: re.Match[str]) -> bool:
    if pattern not in _MEDICAL_PATTERNS or _current_state_reported(message):
        return False
    start, end = _context_bounds(_CONDITION_BREAK, message, match)
    prefix = message[start : match.start()]
    frame = list(_HYPOTHETICAL_FRAME.finditer(prefix))
    current_prefix = prefix[frame[-1].end() :] if frame else prefix
    reported = bool(_COMPLETED_EVENT.search(match.group()))
    if _CURRENT_EVENT.search(prefix) and not frame:
        return False
    if reported and (not frame or _CURRENT_EVENT.search(current_prefix)):
        return False
    # A conditional at the end governs its coordinated symptom list, not an actual prior event.
    return any(
        later.start() >= match.start() and _conditional_suffix(message[later.end() : end])
        for cue in _MEDICAL_PATTERNS
        for later in cue.finditer(message, start, end)
    )


def _sudden_assertion(message: str, match: re.Match[str]) -> bool:
    start, _ = _context_bounds(_CLAUSE_BREAK, message, match)
    return bool(re.search(_SUDDEN, message[start : match.end()]))


def _stable_baseline(
    pattern: re.Pattern[str], message: str, match: re.Match[str], start: int, end: int
) -> bool:
    if pattern not in (_NEUROLOGICAL, _BREATHING, _VISION_LOSS):
        return False
    clause = message[start:end]
    prefix = message[start : match.start()]
    baseline = _BASELINE_DURATION.search(clause)
    tied_baseline = bool(_USUAL_PREFIX.search(prefix))
    if baseline:
        inside_symptom = match.start() - start <= baseline.start() < match.end() - start
        intervening = message[start + baseline.end() : match.start()]
        tied_baseline |= inside_symptom or bool(
            baseline.end() <= len(prefix)
            and _BASELINE_ADVERBS.fullmatch(intervening.replace(" ", ""))
        )
        if baseline.start() >= match.end() - start:
            preceding = message[match.end() : start + baseline.start()]
            tied_baseline |= bool(_BASELINE_SUFFIX.fullmatch(preceding))
    if not tied_baseline:
        return False
    if _sudden_assertion(message, match):
        return False
    sentence_start, sentence_end = _context_bounds(_SENTENCE_BREAK, message, match)
    if _asserted_matches(_NEW_OR_WORSE, message[sentence_start:sentence_end]):
        return False
    if baseline and _CURRENT_EVENT.search(prefix, min(baseline.end(), len(prefix))):
        if not _UNCHANGED.search(clause):
            return False
    return True


def _asserted_matches(pattern: re.Pattern[str], message: str) -> list[re.Match[str]]:
    matches = []
    for match in pattern.finditer(message):
        start, end = _context_bounds(_CLAUSE_BREAK, message, match)
        prefix, suffix = message[start : match.start()], message[match.end() : end]
        if _denied(suffix):
            continue
        if pattern is _DIAPHORESIS and _SWEAT_DENIED_SUFFIX.search(suffix):
            continue
        hypothetical = _conditional_suffix(suffix) or _linked_condition(pattern, message, match)
        if hypothetical and not (pattern in _MEDICAL_PATTERNS and _current_state_reported(message)):
            continue
        if _REFERENCE_SUFFIX.search(suffix):
            continue
        history = _REMOTE_HISTORY.search(prefix + match.group() + suffix)
        if history and not _CURRENT.search(prefix):
            continue
        if _stable_baseline(pattern, message, match, start, end):
            continue
        matches.append(match)
    return matches


def _has_upper_body_warning_combination(message: str) -> bool:
    signs = [
        (kind, match)
        for kind, pattern in enumerate((_UPPER_BODY_DISCOMFORT, _DIAPHORESIS, _BREATHLESSNESS))
        for match in _asserted_matches(pattern, message)
    ]
    latest = {}
    for kind, match in sorted(signs, key=lambda sign: sign[1].start()):
        latest[kind] = match
        if len(latest) < 3:
            continue
        start = min(sign.start() for sign in latest.values())
        end = max(sign.end() for sign in latest.values())
        if end - start > 240:
            continue
        # These weaker signs only route together, not as isolated or stable chronic symptoms.
        before = list(re.finditer(r"[.!?;\n]", message[:start]))
        after = re.search(r"[.!?;\n]", message[end:])
        episode_start = before[-1].end() if before else 0
        episode_end = end + after.start() if after else len(message)
        episode = message[episode_start:episode_end]
        if _STABLE_COMBINATION.search(episode):
            acute = any(_sudden_assertion(message, sign) for sign in latest.values())
            if not acute and not _asserted_matches(_NEW_OR_WORSE, episode):
                continue
        return True
    return False


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
    """Route explicit red flags locally; absence of a match is not a safety assessment."""
    medical_context = [
        context for pattern, context in _MEDICAL_ROUTES if _asserted_matches(pattern, message)
    ]
    for match in _asserted_matches(_VISION_LOSS, message):
        start, end = _context_bounds(_SENTENCE_BREAK, message, match)
        if _VISION_ONSET.search(message[start:end]):
            medical_context.append(
                "갑자기 시력을 잃거나 한쪽 눈이 전혀 보이지 않는 상황은 "
                "즉시 진료가 필요합니다. 직접 운전하지 마세요."
            )
            break
    impacts = _asserted_matches(_HEAD_IMPACT, message)
    warnings = _asserted_matches(_HEAD_WARNING, message)
    if any(
        abs(impact.start() - warning.start()) <= 240 for impact in impacts for warning in warnings
    ):
        medical_context.insert(
            0,
            "머리를 부딪친 뒤 반복해서 토하거나 깨우기 어려운 모습은 "
            "긴급한 확인이 필요할 수 있습니다.",
        )
    if _has_upper_body_warning_combination(message):
        medical_context.append(
            "명치나 상체의 불편감에 식은땀과 숨참이 함께 나타나는 상황은 "
            "긴급한 확인이 필요할 수 있습니다."
        )
    self_harm = bool(_asserted_matches(_SELF_HARM, message))
    if not medical_context and not self_harm:
        return None

    parts = []
    if medical_context:
        parts.append(
            " ".join(medical_context)
            + "\n원인은 채팅으로 판단할 수 없습니다. 지금 즉시 119에 전화해 도움을 요청해 주세요."
        )
        if _asserted_matches(_CHOKING, message):
            parts.append(
                "전화는 스피커폰으로 켜고 목에 음식이 걸린 상황과 호흡·의식 상태를 알리세요. "
                "현장 응급처치는 119 상담원의 안내를 따르세요."
            )
    if self_harm:
        parts.append(
            "스스로를 해칠 위험에 관한 말씀을 가볍게 넘기지 않겠습니다. "
            "자살예방상담전화 109로 지금 연락해 주세요. "
            "당장 스스로를 해칠 위험이 있거나 이미 다쳤다면 즉시 119 또는 112에 전화해 주세요."
        )
    parts.append(
        "채팅 답변을 기다리지 말고, 보호자나 주변 사람에게 지금 함께 있어 달라고 부탁해 주세요."
    )
    parts.append("이 서비스는 신고하거나 위치를 전송하지 않습니다. 직접 전화해 주세요.")
    return ChatResponse(
        answer="\n\n".join(parts),
        mode="emergency",
        notice=NOTICE,
    )
