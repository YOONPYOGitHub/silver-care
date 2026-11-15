import asyncio
import logging
import re
import ssl
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx
from pydantic import SecretStr

from silvercare.models import HealthArticle, HealthSection, HealthTopic, Message

logger = logging.getLogger(__name__)
ENDPOINT = "https://api.kdca.go.kr/api/provide/healthInfo"
SOURCE = (
    "https://health.kdca.go.kr/healthinfo/biz/health/gnrlzHealthInfo/"
    "gnrlzHealthInfo/gnrlzHealthInfoView.do?cntnts_sn="
)
MAX_BYTES = 1024 * 1024


class TokenLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "api.kdca.go.kr" in message:
            record.msg = re.sub(r"(?i)([?&]TOKEN=)[^&\s\"']+", r"\1[REDACTED]", message)
            record.args = ()
        return True


# The provider only accepts GET; keep its query credential out of HTTP request logs.
logging.getLogger("httpx").addFilter(TokenLogFilter())


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    pattern: str
    exclude_pattern: str = ""

    def public(self) -> HealthTopic:
        return HealthTopic(id=self.id, title=self.title, url=SOURCE + self.id)


TOPICS = (
    Topic("6752", "건강노화", r"건강노화|건강한 노후|건강하게 지내"),
    Topic("6722", "근 손실", r"근 손실|근손실|근감소"),
    Topic("1743", "낙상", r"낙상|넘어지|넘어질"),
    Topic(
        "6698",
        "노인 고혈압",
        r"고혈압|혈압",
        r"^(?!.*고혈압).*(?:저혈압|혈압.{0,8}(?:낮|떨어))",
    ),
    Topic("5306", "노인 당뇨병", r"당뇨|혈당"),
    Topic("2447", "노인 변비", r"변비"),
    Topic("6695", "노인 보행장애", r"보행장애|걷기 어렵|걷기가 어렵|보행이 불편"),
    Topic(
        "5314",
        "노인 삼킴장애",
        r"삼킴|삼키기|연하장애|연하곤란|사레|음식.{0,12}목에.{0,10}(?:걸리|걸려|걸린)",
    ),
    Topic(
        "5428",
        "노인 약물복용",
        r"약물|복약|복용|혈압약|감기약|당뇨약|처방약|"
        r"(?<![가-힣])약(?:을|은|이|의|과|도|하고|\s|$)",
    ),
    Topic("6766", "노인 어지럼증", r"어지럼|어지러"),
    Topic("5783", "노인 요실금", r"요실금|소변이 새|소변을 참기"),
    Topic("5489", "노인성 난청", r"난청|잘 안 들|보청기"),
    Topic("6761", "노인의 영양", r"영양|식단|식사|입맛"),
    Topic("5833", "골다공증", r"골다공"),
    Topic("6876", "골관절염", r"골관절염|퇴행성 관절염"),
    Topic("6306", "안구건조증", r"안구건조|눈이 건조|눈이 뻑뻑"),
    Topic("6551", "탈수", r"탈수|수분 섭취|물 마시"),
    Topic("6308", "수면무호흡증", r"수면무호흡|수면 무호흡|코골이|코를 골"),
    Topic("6788", "우울감", r"우울"),
    Topic(
        "6858",
        "치매환자의 가족을 위한 정보",
        r"(?=.*치매)(?=.*(?:돌봄|돌보|가족|부모|어머니|아버지))",
    ),
    Topic("5969", "무릎관절염, 올바로 운동하기", r"무릎.*(?:운동|관절염)|관절염.*운동"),
)
BY_ID = {topic.id: topic for topic in TOPICS}


def select_topics(message: str, history: list[Message]) -> list[HealthTopic]:
    def matching(text: str) -> list[HealthTopic]:
        matches = [
            topic.public()
            for topic in TOPICS
            if re.search(topic.pattern, text)
            and not (topic.exclude_pattern and re.search(topic.exclude_pattern, text))
        ]
        return sorted(matches, key=lambda topic: topic.id != "5428")

    matches = matching(message)
    if matches:
        return matches[:3]
    if re.fullmatch(r"\s*(?:이거|그거|그 내용|더|좀 더|자세히)[^\n]{0,20}", message):
        previous = next((item.content for item in reversed(history) if item.role == "user"), "")
        return matching(previous)[:3]
    return []


def article_evidence(article: HealthArticle, query: str) -> str:
    def terms(text: str) -> set[str]:
        words = re.findall(r"[가-힣A-Za-z0-9]{2,}", text.casefold())
        return {
            term
            for word in words
            for term in [word, *(word[i : i + 2] for i in range(len(word) - 1))]
        }

    keywords = terms(query)
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for section in article.sections:
        for paragraph in section.text.splitlines():
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            for offset in range(0, len(paragraph), 1400):
                text = paragraph[offset : offset + 1400]
                if text in seen:
                    continue
                seen.add(text)
                score = len(keywords & terms(text)) + 3 * len(keywords & terms(section.title))
                candidates.append((score, len(candidates), f"{section.title}\n{text}"))
    relevant = [item for item in candidates if item[0] > 0] or candidates
    selected = sorted(
        sorted(relevant, key=lambda item: (-item[0], item[1]))[:3], key=lambda item: item[1]
    )
    return "\n\n".join(item[2] for item in selected)[:4800]


class HealthUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Official health information unavailable")


class OriginalText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "table", "iframe", "object"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "table", "iframe", "object"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def parse_article(data: bytes, topic: Topic) -> HealthArticle:
    if len(data) > MAX_BYTES:
        raise HealthUnavailable("response_limit")
    try:
        text = data.decode("utf-8-sig")
        if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
            raise HealthUnavailable("unsafe_xml")
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise HealthUnavailable("invalid_xml") from None
    if root.tag != "XML":
        raise HealthUnavailable("invalid_xml")

    def value(path: str) -> str:
        return (root.findtext(path) or "").strip()

    if value("./HEAD/CODE") != "S001":
        raise HealthUnavailable("provider_denied")
    if value("./svc/CNTNTS_SN") != topic.id or value("./svc/CNTNTSSJ") != topic.title:
        raise HealthUnavailable("content_identity")
    sections: list[HealthSection] = []
    previous_section: tuple[str, str] | None = None
    for element in root.findall("./svc/cntntsClList/cntntsCl"):
        title = (element.findtext("CNTNTS_CL_NM") or "").strip()
        original = (element.findtext("CNTNTS_CL_CN") or "").strip()
        if not title or not original or title.startswith(("참고문헌", "연관")):
            continue
        if re.fullmatch(r"https?://\S+", original):
            continue
        parser = OriginalText()
        parser.feed(original)
        parser.close()
        content = re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()
        if content:
            section_id = (element.findtext("CNTNTSCLSN") or "").strip()
            identity = (section_id, title)
            if section_id and previous_section == identity:
                sections[-1].text += "\n\n" + content
            else:
                sections.append(HealthSection(title=title, text=content))
            previous_section = identity
    if not sections:
        raise HealthUnavailable("empty_content")
    if len(sections) > 80 or sum(len(section.text) for section in sections) > 250000:
        raise HealthUnavailable("content_limit")
    return HealthArticle(
        topic=topic.public(), sections=sections, fetched_at=datetime.now(UTC).isoformat()
    )


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return context


class HealthService:
    def __init__(self, key: SecretStr, client: httpx.AsyncClient | None = None) -> None:
        self.key = key
        self.client = client
        self.cache: OrderedDict[str, tuple[float, HealthArticle]] = OrderedDict()
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def article(self, content_id: str) -> HealthArticle:
        if content_id not in BY_ID:
            raise ValueError("Unsupported health topic")
        if not self.key.get_secret_value():
            raise HealthUnavailable("not_configured")
        async with self.lock:
            cached = self.cache.get(content_id)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(content_id)
                return cached[1]
            if self.client is None:
                context = await asyncio.to_thread(tls_context)
                self.client = httpx.AsyncClient(
                    timeout=httpx.Timeout(30, connect=10), verify=context, follow_redirects=False
                )
            try:
                async with self.client.stream(
                    "GET",
                    ENDPOINT,
                    params={"TOKEN": unquote(self.key.get_secret_value()), "cntntsSn": content_id},
                ) as response:
                    if not response.is_success:
                        raise HealthUnavailable("provider_http")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise HealthUnavailable("response_limit")
                result = parse_article(bytes(data), BY_ID[content_id])
            except httpx.HTTPError:
                logger.warning("Official health API transport failed")
                raise HealthUnavailable("transport") from None
            except HealthUnavailable as exc:
                logger.warning("Official health API content failed: %s", exc.reason)
                raise
            self.cache[content_id] = (time.monotonic() + 3600, result)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result
