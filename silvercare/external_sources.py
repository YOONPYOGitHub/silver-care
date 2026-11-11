"""Bounded public-reference collection; does not ingest or modify a search index.

Use ``--providers`` to select fixed document/snapshot collectors; the default
includes the care manual, MedlinePlus and welfare/institution API snapshots.
``mohw-129-guidance`` verifies the article's KOGL Type 1 notice. ``129-faq`` and
``kdca-older-adults`` are permission-gated HTML collection paths, not the runtime
KDCA API connector in silvercare.health.
Successful selected providers are refreshed; unselected or failed documents are
retained, not cleared for publication. Errors produce exit status 1. Raw downloads
remain private. Preparation is not expert review or reuse clearance.
"""

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, quote_plus, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

from silvercare.config import DataSourceSettings
from silvercare.data import SearchDocument, chunks
from silvercare.privacy import normalize, redact

MOHW_PAGE = "https://www.mohw.go.kr/board.es?mid=a10411010100&bid=0019&act=view&list_no=1491777"
MOHW_DOWNLOAD = "https://www.mohw.go.kr/boardDownload.es?bid=0019&list_no=1491777&seq=3"
MEDLINE_PAGE = "https://medlineplus.gov/xml.html"
FAQ_129_PAGE = "https://www.129.go.kr/faq/faq05.do"
MOHW_129_GUIDE = (
    "https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027&act=view&list_no=1485141"
)
KDCA_VIEW = (
    "https://health.kdca.go.kr/healthinfo/biz/health/gnrlzHealthInfo/"
    "gnrlzHealthInfo/gnrlzHealthInfoView.do"
)
# IDs and titles verified on the public older-adult listing; no crawling/pagination.
KDCA_OLDER_ARTICLES = (
    ("1743", "낙상"),
    ("6758", "노인 운동"),
    ("6761", "노인의 영양"),
    ("5428", "노인 약물복용"),
)
MAX_FAQ_RECORDS = 10
MAX_KDCA_SECTIONS = 24
NHIS_DETAIL = "https://apis.data.go.kr/B550928/getLtcInsttDetailInfoService02"
OLD_NHIS_DETAIL = "https://apis.data.go.kr/B550928/longTermCareInfoService01"
MAX_DOWNLOAD = 40 * 1024 * 1024
MAX_EXPANDED = 120 * 1024 * 1024
MAX_DETAILS = 10  # Two welfare services share a total budget of 20.
MAX_NHIS_DETAILS = 20
MAX_LIST = 20
API_DOCS = {
    "welfare-central": "15090532",
    "welfare-local": "15108347",
    "nhis-search": "15059029",
    "nhis-detail": "15058856",
}
DEFAULT_PROVIDERS = ("mohw-care-2026", "medlineplus", *API_DOCS)
PUBLIC_PROVIDERS = ("129-faq", "kdca-older-adults", "mohw-129-guidance")
ALL_PROVIDERS = (*DEFAULT_PROVIDERS, *PUBLIC_PROVIDERS)
PUBLIC_REUSE_NOTICES = {
    "129-faq": {
        "terms_url": "https://www.mohw.go.kr/menu.es?mid=a10103010400",
        "source_notice_url": FAQ_129_PAGE,
        "action": (
            "No KOGL mark was found on the 129 FAQ. MOHW's policy requires prior "
            "consultation for unmarked works. Contact its public-work officer "
            "(044-202-2158), confirm coverage of these FAQs, and supply written "
            "permission/scope for reproduction, chunking, public indexing, generated "
            "answers and commercial use if applicable. Retained copies are not cleared."
        ),
    },
    "kdca-older-adults": {
        "terms_url": "https://health.kdca.go.kr/healthinfo/biz/health/useGuidance/cpyrhtPolicy.do",
        "license_url": "https://www.kogl.or.kr/info/licenseType4.do",
        "application_url": (
            "https://health.kdca.go.kr/healthinfo/biz/health/portalUseGuidance/"
            "hlthinsReqst/hlthinsReqstMth.do?index=3"
        ),
        "action": (
            "All four articles display KOGL Type 4: attribution, noncommercial, "
            "no derivatives. Attribution alone does not clear public generated/modified "
            "RAG content. Supply KDCA's written permission/scope for this intended use, "
            "including commercial use if applicable. The separate OpenAPI process "
            "requires a user application and purpose review; API approval alone must "
            "not be treated as a waiver of Type 4. Retained copies are not cleared."
        ),
    },
}

# Exact English topic titles, rather than substring matches that select pediatric
# or unrelated Spanish topics. Selection is reproducible across daily archives.
MEDLINE_TOPICS = frozenset(
    """Advance Directives|Alzheimer's Caregivers|Alzheimer's Disease|Anxiety|
Arthritis|Assistive Devices|Balance Problems|Bedsores|Benefits of Exercise|
Blood Pressure Medicines|Bone Health|Bowel Incontinence|Breathing Problems|
Bronchitis|Cardiac Rehabilitation|Caregiver Health|Caregivers|Cataract|
Cholesterol|Chronic Kidney Disease|Chronic Pain|Constipation|COPD|Coronary Artery Disease|
Dehydration|Delirium|Dementia|Dental Health|Depression|Diabetes|Diabetes Type 2|
Diabetic Diet|Diabetic Foot|Diabetic Nerve Problems|Dietary Supplements|
Dizziness and Vertigo|Drug Reactions|Dry Mouth|Edema|Elder Abuse|
End of Life Issues|Exercise and Physical Fitness|Exercise for Older Adults|
Eye Care|Falls|Fatigue|Fiber|Food Safety|Foot Health|Frailty|
Glaucoma|Grief|Healthy Aging|Healthy Sleep|Hearing Aids|
Hearing Disorders and Deafness|Heart Attack|Heart Failure|Heart-Healthy Diet|
Hemorrhagic Stroke|High Blood Pressure|Hip Injuries and Disorders|Hip Replacement|
Home Care Services|Hospice Care|How Much Exercise Do I Need?|Immunization|
Insomnia|Ischemic Stroke|Kidney Diseases|Lewy Body Dementia|Living with Chronic Illness|
Loneliness|Low Vision|Lung Diseases|Macular Degeneration|Malnutrition|
Medical Device Safety|Medicines|Memory|Mental Health|Mobility Aids|
Nutrition|Nutrition for Older Adults|Nutritional Support|Obesity|Older Adult Health|
Older Adult Mental Health|Osteoarthritis|Osteoporosis|Pain|Pain Relievers|
Palliative Care|Parkinson's Disease|Patient Safety|Peripheral Arterial Disease|
Physical Therapy|Pneumonia|Prediabetes|Preventing Infections|Rehabilitation|
Rheumatoid Arthritis|Shingles|Skin Aging|Sleep Apnea|Sleep Disorders|
Social Isolation|Speech and Communication Disorders|Stress|Stroke|
Stroke Rehabilitation|Swallowing Disorders|Taste and Smell Disorders|
Thyroid Diseases|Tooth Disorders|Tremor|Urinary Incontinence|Urinary Tract Infections|
Vision Impairment and Blindness|Vitamin D|Vitamins|Walking Problems|Weight Control|
Wounds and Injuries""".replace("\n", "").split("|")
)


@dataclass(frozen=True)
class Record:
    record_id: str
    title: str
    text: str
    url: str
    year: str = ""
    updated: str = ""
    audience: Literal["general", "self", "caregiver", "provider"] = "general"


class CollectionError(Exception):
    """Only controlled, credential-free messages cross the reporting boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class ProviderReport:
    provider: str
    status: str = "failed"
    records: int = 0
    chunks: int = 0
    duplicates: int = 0
    requests: int = 0
    detail_calls: int = 0
    raw_files: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    audit_requests: list[dict[str, str]] = field(default_factory=list)
    results: list[dict[str, str | int]] = field(default_factory=list)


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_DOWNLOAD or b"<!ENTITY" in data.upper():
        raise CollectionError("unsafe_xml", "XML exceeds limits or declares entities")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise CollectionError("invalid_xml", "Response is not well-formed XML") from None


def xml_value(root: ET.Element, *names: str) -> str:
    return next((normalize(e.text or "") for e in root.iter() if local_name(e) in names), "")


def api_result(data: bytes, status: int) -> tuple[ET.Element, str, str]:
    """Inspect the gateway envelope and application result even on HTTP 200."""
    root = parse_xml(data)
    codes = [
        normalize(e.text or "")
        for e in root.iter()
        if local_name(e) in {"resultCode", "returnReasonCode", "cmmMsgHeader"} and not list(e)
    ]
    message = xml_value(root, "errMsg", "resultMessage", "resultMsg", "returnAuthMsg")
    code = next((value for value in codes if value not in {"0", "00", "0000"}), "")
    if code or status >= 400:
        raise CollectionError(code or f"http_{status}", message or "API request rejected")
    if not codes:
        raise CollectionError("missing_result_code", message or "API status envelope is absent")
    return root, codes[0], message


def zip_members(data: bytes) -> Iterator[tuple[str, bytes]]:
    """Read bounded members in memory, never extract archive paths."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 4000 or sum(m.file_size for m in members) > MAX_EXPANDED:
                raise CollectionError(
                    "archive_limit", "ZIP member count/expanded size exceeds limit"
                )
            for member in members:
                if (
                    member.file_size > MAX_DOWNLOAD
                    or member.flag_bits & 1
                    or member.file_size > max(1, member.compress_size) * 500
                ):
                    raise CollectionError("archive_limit", "ZIP member exceeds safety limits")
            for member in members:
                if member.filename.endswith(".xml") and not member.is_dir():
                    with archive.open(member) as handle:
                        content = handle.read(MAX_DOWNLOAD + 1)
                    if len(content) > MAX_DOWNLOAD:
                        raise CollectionError("archive_limit", "Expanded XML exceeds limit")
                    yield member.filename, content
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise CollectionError("invalid_zip", "Invalid or unsupported ZIP archive") from None


class TextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "li", "br", "h1", "h2", "h3", "tr", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "h1", "h2", "h3", "tr", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = TextHTML()
    parser.feed(value)
    return normalize("".join(parser.parts))


@dataclass
class SourceElement:
    tag: str
    attrs: dict[str, str]
    children: list["SourceElement | str"] = field(default_factory=list)

    def elements(self) -> Iterator["SourceElement"]:
        yield self
        for child in self.children:
            if isinstance(child, SourceElement):
                yield from child.elements()

    def text(self) -> str:
        # Feed scoped elements through the same block/hidden-text rules as other sources.
        parser = TextHTML()

        def walk(element: SourceElement) -> None:
            parser.handle_starttag(element.tag, [])
            for child in element.children:
                if isinstance(child, SourceElement):
                    walk(child)
                else:
                    parser.handle_data(child)
            parser.handle_endtag(element.tag)

        walk(self)
        return normalize("".join(parser.parts))

    def has_class(self, value: str) -> bool:
        return value in self.attrs.get("class", "").split()


class SourceHTML(HTMLParser):
    """Small bounded-page tree for selecting source content, never whole-page text."""

    def __init__(self, data: bytes) -> None:
        super().__init__(convert_charrefs=True)
        if len(data) > MAX_DOWNLOAD:
            raise CollectionError("download_limit", "HTML exceeds download limit")
        self.root = SourceElement("", {})
        self.stack = [self.root]
        try:
            self.feed(data.decode("utf-8-sig"))
            self.close()
        except UnicodeDecodeError:
            raise CollectionError("invalid_html", "Expected public UTF-8 HTML") from None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = SourceElement(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(element)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def source_date(value: str) -> str:
    """Return only an actual calendar date, not a crawl or clinical review date."""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def mohw_129_records(data: bytes) -> list[Record]:
    elements = list(SourceHTML(data).root.elements())
    scopes = [node for node in elements if node.attrs.get("id") == "contents_body"]
    if len(scopes) != 1:
        raise CollectionError("article_identity", "Expected one MOHW content container")
    scope = scopes[0]
    articles = [
        node
        for node in scope.children
        if isinstance(node, SourceElement)
        if node.tag == "article" and node.has_class("board_view")
    ]
    if len(articles) != 1:
        raise CollectionError("article_identity", "Expected one MOHW 129 service article")
    article = articles[0]
    nodes = list(article.elements())
    rights = [
        node
        for node in scope.children
        if isinstance(node, SourceElement) and node.has_class("codeView01")
    ]
    licensed = any(
        "출처표시" in node.text()
        and any(
            image.tag == "img"
            and image.attrs.get("alt") == "공공누리 1유형"
            and urlsplit(image.attrs.get("src", "")).path.endswith("/new_img_opentype01.png")
            for image in node.elements()
        )
        for node in rights
    )
    if not licensed:
        raise CollectionError("reuse_permission_required", "Article-scoped KOGL Type 1 is absent")
    titles = [node.text() for node in nodes if node.tag == "h2"]
    areas = [node for node in nodes if node.has_class("viewArea")]
    stamp = re.search(r"작성일\s*:\s*(\d{4}-\d{2}-\d{2})", article.text())
    updated = source_date(stamp[1]) if stamp else ""
    if (
        len(titles) != 1
        or not all(term in titles[0] for term in ("129", "수어", "채팅", "챗봇"))
        or len(areas) != 1
        or not updated
    ):
        raise CollectionError("article_identity", "MOHW 129 title, body or posting date is absent")
    body = areas[0].text().split("<붙임>", 1)[0].strip()
    if len(body) < 400 or not all(
        term in body for term in ("로그인 없이", "365일 24시간", "누리집")
    ):
        raise CollectionError("incomplete_article", "MOHW 129 service guidance is incomplete")
    text = (
        f"출처: 보건복지부 · 보건복지상담센터, {updated}\n"
        f"제목: {titles[0]}\n"
        "이용조건: 공공누리 제1유형(출처표시). 본문 텍스트만 활용; 첨부·이미지 제외.\n"
        "구분: 여기서 365일 24시간은 챗봇상담 안내이며 모든 상담사 상담시간을 뜻하지 않는다.\n"
        + body
    )
    return [
        Record(
            "1485141",
            titles[0],
            text,
            MOHW_129_GUIDE,
            year=updated[:4],
            updated=updated,
            audience="self",
        )
    ]


def faq_129_records(data: bytes) -> list[Record]:
    elements = list(SourceHTML(data).root.elements())
    by_id = {element.attrs["id"]: element for element in elements if element.attrs.get("id")}
    rows = [
        element
        for element in elements
        if element.tag == "tr" and re.fullmatch(r"faq\d+", element.attrs.get("id", ""))
    ][:MAX_FAQ_RECORDS]
    records: list[Record] = []
    seen: set[str] = set()
    for row in rows:
        record_id = row.attrs["id"].removeprefix("faq")
        if record_id in seen:
            continue
        seen.add(record_id)
        fields = {
            element.attrs["data-label"]: element.text()
            for element in row.elements()
            if element.tag == "td" and "data-label" in element.attrs
        }
        question = fields.get("제목", "")
        detail = by_id.get(f"faqList{record_id}")
        answers = (
            [element.text() for element in detail.elements() if element.has_class("post-con")]
            if detail is not None
            else []
        )
        if not question or len(answers) != 1 or not answers[0]:
            raise CollectionError("incomplete_faq", "FAQ row lacks its matching question/answer")
        posted = source_date(fields.get("작성일", ""))
        category = fields.get("상담분야", "노인장애인")
        title = f"노인장애인 FAQ · {category} · {question}"
        text = f"{title}\n" + (f"게시일: {posted}\n" if posted else "")
        records.append(
            Record(
                record_id,
                title,
                text + f"Q: {question}\nA: {answers[0]}",
                f"{FAQ_129_PAGE}#faq{record_id}",
                posted[:4],
                posted,
                # Public questions mix beneficiary, family and worker qualification guidance.
                audience="general",
            )
        )
    if not records:
        raise CollectionError("empty_faq", "No paired older-adult/disability FAQs found")
    return records


def kdca_records(data: bytes, article_id: str, expected_title: str) -> list[Record]:
    if (article_id, expected_title) not in KDCA_OLDER_ARTICLES:
        raise CollectionError("invalid_selection", "KDCA article is outside the bounded selection")
    elements = list(SourceHTML(data).root.elements())
    title = next(
        (element.text() for element in elements if element.attrs.get("id") == "subContentSj"),
        "",
    )
    if title != expected_title:
        raise CollectionError(
            "article_identity", "Expected KDCA article title is absent or changed"
        )
    date_text = "\n".join(element.text() for element in elements if element.has_class("hd-info"))
    dates = {}
    for label in ("등록일자", "업데이트"):
        match = re.search(rf"{label}\s*:\s*(\d{{4}}-\d{{2}}-\d{{2}})", date_text)
        dates[label] = source_date(match[1]) if match else ""
    date_note = "\n".join(f"{key}: {value}" for key, value in dates.items() if value)
    sections = [
        element
        for element in elements
        if element.has_class("contents-Div")
        and re.fullmatch(r"contentsDiv\d+", element.attrs.get("id", ""))
    ]
    if len(sections) > MAX_KDCA_SECTIONS:
        raise CollectionError("selection_limit", "KDCA article exceeded the section limit")
    records: list[Record] = []
    seen: set[str] = set()
    for section in sections:
        section_id = section.attrs["id"]
        if section_id in seen:
            continue
        seen.add(section_id)
        heading = next(
            (element.text() for element in section.elements() if element.tag == "h3"), ""
        )
        text = section.text()
        if heading == "참고문헌":
            continue
        if not heading or not text.removeprefix(heading).strip():
            raise CollectionError("incomplete_article", "KDCA section lacks heading or body text")
        section_title = f"{title} · {heading}"
        records.append(
            Record(
                f"{article_id}:{section_id}",
                section_title,
                section_title + "\n" + (date_note + "\n" if date_note else "") + text,
                f"{KDCA_VIEW}?cntnts_sn={article_id}#{section_id}",
                dates["등록일자"][:4],
                dates["업데이트"],
                audience="self",
            )
        )
    if not records:
        raise CollectionError("empty_article", "No public KDCA health guidance sections found")
    return records


def medline_records(data: bytes) -> list[Record]:
    records: list[Record] = []
    seen: set[str] = set()
    found_xml = False
    for name, content in zip_members(data):
        if not re.fullmatch(r"mplus_topics_\d{4}-\d{2}-\d{2}\.xml", name):
            continue
        found_xml = True
        root = parse_xml(content)
        stamp = root.attrib.get("date-generated", "")
        try:
            updated = datetime.strptime(stamp.partition(" ")[0], "%m/%d/%Y").date().isoformat()
        except ValueError:
            raise CollectionError(
                "missing_source_date", "MedlinePlus generation date missing"
            ) from None
        for topic in root.findall("health-topic"):
            title = topic.attrib.get("title", "")
            if topic.attrib.get("language") != "English" or title not in MEDLINE_TOPICS:
                continue
            record_id = topic.attrib.get("id", "")
            summary_element = topic.find("full-summary")
            summary = html_text(
                "".join(summary_element.itertext()) if summary_element is not None else ""
            )
            if not summary or not record_id:
                raise CollectionError("incomplete_topic", "Selected topic lacks ID or full summary")
            if record_id in seen:
                continue
            seen.add(record_id)
            aliases = [
                normalize("".join(e.itertext()))
                for e in topic
                if e.tag in {"also-called", "see-reference"}
            ]
            text = title + "\n"
            if aliases:
                text += "Also called: " + "; ".join(dict.fromkeys(aliases)) + "\n"
            records.append(
                Record(
                    record_id,
                    title,
                    text + summary,
                    topic.attrib.get("url", ""),
                    updated[:4],
                    updated,
                )
            )
    if not found_xml or not records:
        raise CollectionError("empty_selection", "No selected English MedlinePlus topics found")
    if len(records) > 150:
        raise CollectionError("selection_limit", "MedlinePlus selection exceeded 150 topics")
    return records


GUIDE_SECTIONS = {
    1: "Ⅰ 노인맞춤돌봄서비스 추진 개요",
    2: "Ⅱ 노인맞춤돌봄서비스 세부 내용",
    3: "Ⅲ 노인맞춤돌봄서비스 지역수행기관 선정",
    4: "Ⅳ 노인맞춤돌봄서비스 사업관리",
    5: "Ⅴ 노인맞춤돌봄서비스 제공 서비스",
    6: "Ⅵ 퇴원환자 단기집중 서비스",
}


def paragraph_text(element: ET.Element) -> str:
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        kind = local_name(node)
        if kind in {"tbl", "header", "footer", "pic", "shape", "drawText"}:
            return
        if kind == "t":
            parts.append("".join(node.itertext()))
            return
        if kind in {"lineBreak", "tab"}:
            parts.append(" ")
        for child in node:
            walk(child)

    walk(element)
    return normalize("".join(parts))


def hwpx_records(data: bytes, updated: str = "2026-09-02") -> list[Record]:
    """Keep native paragraphs and labeled table rows, excluding TOC/blank forms."""
    records: list[Record] = []
    sections: dict[int, ET.Element] = {}
    for name, content in zip_members(data):
        match = re.fullmatch(r"Contents/section(\d+)\.xml", name)
        if match:
            sections[int(match[1])] = parse_xml(content)
    if not all(number in sections for number in GUIDE_SECTIONS):
        raise CollectionError("guide_schema", "Expected six substantive HWPX sections are missing")

    def parse_section(number: int, section_title: str) -> None:
        heading = ""

        def emit(text: str, context: str = "") -> None:
            text = normalize(text)
            if len(re.sub(r"[\W\d_]", "", text)) < 8:
                return
            title = section_title + (f" · {heading}" if heading else "")
            content = title + "\n" + (context + "\n" if context else "") + text
            digest = hashlib.sha256(content.encode()).hexdigest()[:20]
            records.append(
                Record(
                    f"section{number}:{digest}",
                    title,
                    content,
                    MOHW_PAGE,
                    "2026",
                    updated,
                )
            )

        def walk(node: ET.Element) -> None:
            nonlocal heading
            kind = local_name(node)
            if kind in {"header", "footer", "pic", "drawText"}:
                return
            if kind == "p":
                text = paragraph_text(node)
                style = node.attrib.get("styleIDRef", "")
                if (
                    text
                    and len(text) < 110
                    and (
                        style in {"6", "13", "20"}
                        or (style == "2" and len(text) < 60)
                        or re.match(r"^(?:\d+[.)]|[가-하]\.)\s+\S", text)
                    )
                ):
                    heading = text
                emit(text)
                for child in node:
                    if local_name(child) != "t":
                        walk_tables(child)
                return
            if kind == "tbl":
                table(node)
                return
            for child in node:
                walk(child)

        def walk_tables(node: ET.Element) -> None:
            if local_name(node) == "tbl":
                table(node)
            elif local_name(node) not in {"header", "footer", "pic", "drawText"}:
                for child in node:
                    walk_tables(child)

        def table(node: ET.Element) -> None:
            rows = [child for child in node if local_name(child) == "tr"]
            if not rows:
                return
            grid: list[list[str]] = []
            cells_by_row: list[list[ET.Element]] = []
            for row_element in rows:
                cells = [child for child in row_element if local_name(child) == "tc"]
                cells_by_row.append(cells)
                grid.append(
                    [
                        "\n".join(
                            text
                            for p in cell.iter()
                            if local_name(p) == "p"
                            if (text := paragraph_text(p))
                        )
                        for cell in cells
                    ]
                )
            # Single-cell boxes and nested tables are layout, not relational rows.
            if (
                max((len(row) for row in grid), default=0) <= 1
                or any(
                    local_name(e) == "tbl"
                    for cells in cells_by_row
                    for cell in cells
                    for e in cell.iter()
                )
                or any(len(text) > 1400 for row in grid for text in row)
            ):
                for cells in cells_by_row:
                    for cell in cells:
                        for child in cell:
                            walk(child)
                return
            header = " | ".join(grid[0])
            previous_label = ""
            for index, row_values in enumerate(grid):
                if not any(row_values):
                    continue
                if row_values[0]:
                    previous_label = row_values[0][:200]
                elif previous_label:
                    row_values[0] = previous_label + " (상위 행)"
                emit(" | ".join(row_values), f"표 머리행: {header}" if index else "")

        walk(sections[number])

    for number, section_title in GUIDE_SECTIONS.items():
        parse_section(number, section_title)
    if not records:
        raise CollectionError("empty_guide", "No meaningful HWPX paragraphs or rows found")
    return records


def safe_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not re.search(r"servicekey|apikey|access_token", parsed.query, re.I)
    )


def document_records(
    records: list[Record],
    dataset: str,
    agency: str,
    license_text: str,
) -> tuple[list[SearchDocument], int]:
    documents: list[SearchDocument] = []
    seen: set[str] = set()
    duplicate_count = 0
    for record in records:
        if not safe_url(record.url):
            raise CollectionError("invalid_provenance", "Missing or unsafe public provenance URL")
        text = redact(record.text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen:
            duplicate_count += 1
            continue
        seen.add(digest)
        parts: Iterator[str]
        if dataset == "129-faq":
            context, separator, answer = text.partition("\nA: ")
            prefix = context + separator
            if not separator or len(prefix) > 1000:
                raise CollectionError(
                    "incomplete_faq", "FAQ question context is missing or too long"
                )
            parts = (prefix + part for part in chunks(answer, size=1600 - len(prefix), overlap=160))
        else:
            parts = chunks(text, size=1600, overlap=160)
        for part in parts:
            identity = hashlib.sha256(f"{dataset}:{record.record_id}:{part}".encode()).hexdigest()
            documents.append(
                SearchDocument(
                    id=identity,
                    title=redact(record.title),
                    content=part,
                    source_url=record.url,
                    dataset_id=dataset,
                    evidence_kind="reference",
                    license=license_text,
                    updated_at=record.updated,
                    source_record_id=record.record_id,
                    source_display=agency,
                    source_year=record.year,
                    review_status="automated",
                    audience=record.audience,
                )
            )
    if not documents:
        raise CollectionError("empty_corpus", "Provider produced no usable reference chunks")
    return documents, duplicate_count


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    # Open with restrictive permissions before writing, including on first creation.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_bytes(data)


class Collector:
    def __init__(self, client: httpx.Client, settings: DataSourceSettings, raw: Path) -> None:
        self.client = client
        self.settings = settings
        self.raw = raw
        self.secrets = tuple(
            value
            for secret in (
                settings.welfare_central_api_key,
                settings.welfare_local_api_key,
                settings.nhis_ltc_search_api_key,
                settings.nhis_ltc_detail_api_key,
            )
            for value in (
                secret.get_secret_value(),
                unquote(secret.get_secret_value()),
                quote(unquote(secret.get_secret_value()), safe=""),
                quote_plus(unquote(secret.get_secret_value()), safe=""),
            )
            if value
        )

    def safe_message(self, message: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            message = message.replace(secret, "[credential removed]")
        message = re.sub(r"https?://\S+", "[URL removed]", message)
        return redact(message)[:500]

    def fetch(
        self,
        report: ProviderReport,
        url: str,
        filename: str,
        params: dict[str, str] | None = None,
    ) -> tuple[bytes, int]:
        if not safe_url(url):
            raise CollectionError(
                "invalid_endpoint", "Only unsigned public HTTPS endpoints are allowed"
            )
        report.requests += 1
        deadline = time.monotonic() + 180
        try:
            # No redirects on signed API calls: never forward credentials to a new host.
            with self.client.stream(
                "GET",
                url,
                params=params,
                follow_redirects=params is None,
            ) as response:
                body = bytearray()
                for block in response.iter_bytes(chunk_size=64 * 1024):
                    if time.monotonic() > deadline:
                        raise CollectionError("download_timeout", "Response exceeded time budget")
                    body.extend(block)
                    if len(body) > MAX_DOWNLOAD:
                        raise CollectionError("download_limit", "Response exceeds download limit")
                data = bytes(body)
                status = response.status_code
        except httpx.HTTPError:
            raise CollectionError(
                "transport_error", "HTTPS request failed; credentials withheld"
            ) from None
        path = self.raw / filename
        private_write(path, data)
        report.raw_files.append(str(path))
        if params is None and not 200 <= status < 300:
            raise CollectionError(f"http_{status}", "Public download failed")
        return data, status

    def api(
        self,
        report: ProviderReport,
        url: str,
        filename: str,
        key: str,
        params: dict[str, str],
    ) -> ET.Element:
        if not key:
            raise CollectionError("missing_key", "Configured service credential is absent")
        unsigned_url = url + ("?" + urlencode(params) if params else "")
        if not safe_url(unsigned_url):
            raise CollectionError("invalid_endpoint", "Audit URL must not contain credentials")
        report.audit_requests.append(
            {
                "unsigned_url": unsigned_url,
                "raw_file": str(self.raw / filename),
            }
        )
        data, status = self.fetch(
            report,
            url,
            filename,
            {"serviceKey": unquote(key), **params},
        )
        try:
            root, code, message = api_result(data, status)
        except CollectionError as error:
            report.results.append(
                {
                    "http_status": status,
                    "code": self.safe_message(error.code),
                    "message": self.safe_message(error.message),
                }
            )
            raise
        report.results.append(
            {
                "http_status": status,
                "code": code,
                "message": self.safe_message(message),
            }
        )
        return root

    def official_docs(self, report: ProviderReport) -> str:
        number = API_DOCS[report.provider]
        url = f"https://www.data.go.kr/data/{number}/openapi.do"
        self.fetch(report, url, f"official-{number}.html")
        return url

    def check_public_reuse(self, report: ProviderReport) -> None:
        if report.provider in PUBLIC_REUSE_NOTICES:
            raise CollectionError(
                "reuse_permission_required",
                "Permission covering public indexing and generated/modified RAG reuse "
                "has not been supplied; no new content will be collected",
            )

    def faq_129(self, report: ProviderReport) -> list[Record]:
        self.check_public_reuse(report)
        data, _ = self.fetch(report, FAQ_129_PAGE, "129-faq05.html")
        records = faq_129_records(data)
        report.notes.extend(
            [
                "One current 노인장애인 FAQ page only; at most 10 Q+A pairs; no pagination. "
                "Row IDs pair each question with its own post-con answer; navigation excluded.",
                "작성일 is a posting date, not clinical review. Mixed public beneficiary/family/"
                "qualification guidance retains general audience; no staff-only classification.",
                "No KOGL mark verified on this FAQ. MOHW requires prior consultation "
                "for unmarked works; public RAG permission remains required.",
            ]
        )
        return records

    def mohw_129_guidance(self, report: ProviderReport) -> list[Record]:
        data, _ = self.fetch(report, MOHW_129_GUIDE, "mohw-129-1485141.html")
        records = mohw_129_records(data)
        report.notes.extend(
            [
                "One fixed MOHW service guide only, with article-scoped KOGL Type 1 verified "
                "on every fetch. Public beneficiary guidance, not the 129 FAQ answer corpus.",
                "Posting date is not a last-reviewed date. Attachments, images and adjacent "
                "posts are excluded. The 24-hour statement concerns chatbot service only.",
            ]
        )
        return records

    def kdca_older_adults(self, report: ProviderReport) -> list[Record]:
        self.check_public_reuse(report)
        records: list[Record] = []
        for article_id, expected_title in KDCA_OLDER_ARTICLES:
            try:
                data, _ = self.fetch(
                    report,
                    f"{KDCA_VIEW}?cntnts_sn={article_id}",
                    f"kdca-{article_id}.html",
                )
                records.extend(kdca_records(data, article_id, expected_title))
            except CollectionError as error:
                report.errors.append(self.error(report, error))
        report.notes.extend(
            [
                "Four verified public older-adult articles only: 낙상(1743), 노인 운동(6758), "
                "노인의 영양(6761), 노인 약물복용(5428); no crawling, API, or pagination.",
                "Only titled contents-Div sections; navigation and 참고문헌 excluded. "
                "등록일자/업데이트 are publisher dates, not clinical review dates. "
                "Missing dates remain blank. Public consumer guidance is self audience.",
                "All four articles display KOGL Type 4: attribution, noncommercial, "
                "no derivatives. Public generated/modified RAG reuse is not cleared.",
            ]
        )
        return records

    def welfare(self, report: ProviderReport, central: bool) -> list[Record]:
        docs_url = self.official_docs(report)
        endpoint = (
            self.settings.welfare_central_api_endpoint
            if central
            else self.settings.welfare_local_api_endpoint
        ).rstrip("/")
        operation = "NationalWelfarelistV001" if central else "LcgvWelfarelist"
        api_key = (
            self.settings.welfare_central_api_key
            if central
            else self.settings.welfare_local_api_key
        ).get_secret_value()
        params = {
            "pageNo": "1",
            "numOfRows": str(MAX_LIST),
            "age": "70",
            "srchKeyCode": "003",
            "searchWrd": "노인",
        }
        if central:
            params["callTp"] = "L"
        else:
            params["ctpvNm"] = "서울특별시"
        root = self.api(
            report,
            endpoint + "/" + operation,
            report.provider + "-list.xml",
            api_key,
            params,
        )
        items = [e for e in root.iter() if local_name(e) == "servList"][:MAX_LIST]
        if not items:
            raise CollectionError("empty_list", "Elderly-welfare query returned no services")
        report.notes.append("Bounded elderly query: age=70, searchWrd=노인; at most 10 details.")
        records: list[Record] = []
        seen: set[str] = set()
        details_available = True
        for item in items:
            record_id = xml_value(item, "servId")
            title = xml_value(item, "servNm")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            detail = item
            if details_available and report.detail_calls < MAX_DETAILS:
                report.detail_calls += 1
                detail_params = {"servId": record_id}
                if central:
                    detail_params["callTp"] = "D"
                try:
                    detail = self.api(
                        report,
                        endpoint
                        + ("/NationalWelfaredetailedV001" if central else "/LcgvWelfaredetailed"),
                        f"{report.provider}-detail-{report.detail_calls:02}.xml",
                        api_key,
                        detail_params,
                    )
                except CollectionError as error:
                    report.errors.append(self.error(report, error))
                    # Preserve factual list entries; do not disguise missing details.
                    details_available = False
            fields = {
                "servDgst": "서비스 요약",
                "wlfareInfoOutlCn": "서비스 개요",
                "tgtrDtlCn": "지원대상",
                "sprtTrgtCn": "지원대상",
                "slctCritCn": "선정기준",
                "alwServCn": "지원내용",
                "applmetList": "신청방법",
                "aplyMtdCn": "신청방법",
                "aplyMtdNm": "신청방법",
                "lifeArray": "생애주기",
                "lifeNmArray": "생애주기",
                "enfcBgngYmd": "시행시작일",
                "enfcEndYmd": "시행종료일",
            }
            text = [title]
            for element in detail.iter():
                if local_name(element) in fields:
                    value = html_text(" ".join(element.itertext()))
                    if value:
                        text.append(f"{fields[local_name(element)]}: {value}")
            updated = xml_value(detail, "lastModYmd")
            if re.fullmatch(r"\d{8}", updated):
                updated = f"{updated[:4]}-{updated[4:6]}-{updated[6:]}"
            else:
                updated = ""
            year = xml_value(detail, "crtrYr")
            if not re.fullmatch(r"\d{4}", year):
                year = ""
            url = xml_value(item, "servDtlLink")
            if (
                not url
                or urlsplit(url).hostname == "apis.data.go.kr"
                or url.startswith(endpoint + "/")
            ):
                url = docs_url
            if len(text) < 2:
                raise CollectionError("incomplete_welfare", "Service has no factual content")
            records.append(Record(record_id, title, "\n".join(text), url, year, updated))
        return records

    def nhis_search(self, report: ProviderReport) -> tuple[list[Record], list[ET.Element]]:
        docs_url = self.official_docs(report)
        endpoint = (
            self.settings.nhis_ltc_search_api_endpoint.rstrip("/") + "/getLtcInsttSeachList02"
        )
        params = {
            "siDoCd": "11",
            "siGunGuCd": "680",
            "pageNo": "1",
            "numOfRows": str(MAX_LIST),
        }
        root = self.api(
            report,
            endpoint,
            "nhis-search-list.xml",
            self.settings.nhis_ltc_search_api_key.get_secret_value(),
            params,
        )
        items = [e for e in root.iter() if local_name(e) == "item"][:MAX_LIST]
        if not items:
            raise CollectionError(
                "empty_list", "Seoul Gangnam-gu NHIS search returned no institutions"
            )
        report.notes.append(
            "One region: Seoul Gangnam-gu (siDoCd=11, siGunGuCd=680). "
            "Institution codes are identifiers, not phone numbers. Public source links "
            "open the official service documentation; actual unsigned queries remain "
            "in private audit metadata."
        )
        return [self.nhis_record(item, docs_url) for item in items], items

    def nhis_record(self, item: ET.Element, url: str) -> Record:
        record_id = xml_value(item, "longTermAdminSym")
        title = xml_value(item, "adminNm") or "장기요양기관 상세정보"
        if not record_id:
            raise CollectionError("missing_institution_id", "Institution identifier is absent")
        lines = [title]
        for element in item.iter():
            if not list(element) and (element.text or "").strip():
                if local_name(element) not in {"resultCode", "resultMsg", "numOfRows", "pageNo"}:
                    lines.append(f"{local_name(element)}: {html_text(element.text or '')}")
        return Record(record_id, title, "\n".join(lines), url)

    def nhis_details(self, report: ProviderReport, items: list[ET.Element]) -> list[Record]:
        docs_url = self.official_docs(report)
        endpoint = self.settings.nhis_ltc_detail_api_endpoint.rstrip("/")
        if endpoint == OLD_NHIS_DETAIL:
            endpoint = NHIS_DETAIL
            report.notes.append(
                "Legacy configured detail endpoint replaced locally with current official "
                "15058856 endpoint; configuration was not modified."
            )
        records: list[Record] = []
        seen: set[str] = set()
        for item in items[:MAX_NHIS_DETAILS]:
            record_id = xml_value(item, "longTermAdminSym")
            if record_id in seen:
                continue
            seen.add(record_id)
            report.detail_calls += 1
            try:
                root = self.api(
                    report,
                    endpoint + "/getConvInsttDetailInfoList02",
                    f"nhis-detail-{report.detail_calls:02}.xml",
                    self.settings.nhis_ltc_detail_api_key.get_secret_value(),
                    {"longTermAdminSym": record_id, "pageNo": "1", "numOfRows": "1"},
                )
                detail_items = [e for e in root.iter() if local_name(e) == "item"]
                if not detail_items:
                    raise CollectionError("empty_detail", "NHIS detail response contains no item")
                for detail in detail_items[:1]:
                    records.append(self.nhis_record(detail, docs_url))
            except CollectionError as error:
                report.errors.append(self.error(report, error))
                # Authentication errors are service-wide, not a reason for 19 retries.
                if error.code in {"20", "22", "30", "31", "99", "http_403"}:
                    break
        return records

    def error(self, report: ProviderReport, error: CollectionError) -> dict[str, str]:
        result = {
            "code": self.safe_message(error.code),
            "message": self.safe_message(error.message),
        }
        if error.code == "reuse_permission_required" and report.provider in PUBLIC_REUSE_NOTICES:
            result.update(PUBLIC_REUSE_NOTICES[report.provider])
        if error.code in {"20", "30", "31"} and report.provider in API_DOCS:
            result["action"] = (
                "Verify service-specific application/approval and active key registration at "
                f"https://www.data.go.kr/data/{API_DOCS[report.provider]}/openapi.do. "
                "Code 30 confirms an unregistered key for this service, not the account's "
                "approval state. No login or approval was automated."
            )
        return result


def collect(
    output_dir: Path = Path("data/processed"),
    *,
    settings: DataSourceSettings | None = None,
    client: httpx.Client | None = None,
    raw_dir: Path = Path("data/raw/external"),
    providers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Refresh selected providers without discarding unselected/failed references."""
    selected = tuple(dict.fromkeys(DEFAULT_PROVIDERS if providers is None else providers))
    if not selected or any(provider not in ALL_PROVIDERS for provider in selected):
        raise ValueError("Select one or more known external-source providers")
    # Preserve the dependency order without silently running unselected API providers.
    selected = tuple(provider for provider in ALL_PROVIDERS if provider in selected)
    if client is None:
        with httpx.Client(
            timeout=httpx.Timeout(90, connect=30),
            headers={"User-Agent": "silver-care-reference-collector/1.0"},
        ) as owned_client:
            return collect(
                output_dir,
                settings=settings,
                client=owned_client,
                raw_dir=raw_dir,
                providers=selected,
            )
    output = output_dir / "external-knowledge.jsonl"
    previous: list[SearchDocument] = []
    previous_lines: dict[str, str] = {}
    if output.exists():
        try:
            lines = [
                line for line in output.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            previous = [SearchDocument.model_validate_json(line) for line in lines]
            previous_lines = {
                document.id: line for document, line in zip(previous, lines, strict=True)
            }
        except (ValueError, OSError):
            raise CollectionError(
                "invalid_existing_corpus", "Existing corpus cannot be read; refusing to overwrite"
            ) from None
    collector = Collector(client, settings or DataSourceSettings(), raw_dir)
    documents: list[SearchDocument] = []
    reports: list[ProviderReport] = []
    nhis_items: list[ET.Element] = []
    for provider in selected:
        report = ProviderReport(provider)
        reports.append(report)
        agency = "한국사회보장정보원"
        license_text = "공공데이터포털: 이용허락범위 제한 없음; 출처 표시"
        records: list[Record] = []
        try:
            if provider == "mohw-care-2026":
                page, _ = collector.fetch(report, MOHW_PAGE, "mohw-landing.html")
                title = html_text(page.decode("utf-8"))
                if "2026.7.27" not in title:
                    raise CollectionError(
                        "guide_revision", "Expected amended guide effective date missing"
                    )
                data, _ = collector.fetch(report, MOHW_DOWNLOAD, "mohw-care-2026.hwpx")
                stamp_match = re.search(
                    r"작성일</strong>\s*<span>(\d{4}-\d{2}-\d{2})",
                    page.decode("utf-8"),
                )
                if stamp_match is None:
                    raise CollectionError("missing_source_date", "Guide posting date is absent")
                records = hwpx_records(data, updated=stamp_match[1])
                agency = "보건복지부"
                license_text = "공개 게시물; 개별 재이용 조건 미확인; 재이용 전 확인 필요"
                report.notes.extend(
                    [
                        f"Effective 2026-07-27; posting date {stamp_match[1]} "
                        "verified on landing page.",
                        "Native XML sections 1–6 only; TOC section 0 and form "
                        "sections 7–8 excluded. Paragraphs, heading context, "
                        "table headers/rows retained; layout boxes traversed. "
                        "Merged/complex multirow headers and page numbers "
                        "are not fully reconstructed.",
                        "Reuse-rights review remains required; no human or expert review claimed.",
                    ]
                )
            elif provider == "medlineplus":
                page, _ = collector.fetch(report, MEDLINE_PAGE, "medlineplus-xml.html")
                links = re.findall(
                    r"""href=["']([^"']*mplus_topics_compressed_(\d{4}-\d{2}-\d{2})\.zip)["']""",
                    page.decode("utf-8"),
                )
                if not links:
                    raise CollectionError(
                        "missing_archive_link", "No current MedlinePlus ZIP link found"
                    )
                link, stamp = max(links, key=lambda match: match[1])
                url = urljoin(MEDLINE_PAGE, link)
                if urlsplit(url).hostname != "medlineplus.gov":
                    raise CollectionError(
                        "unexpected_archive_host", "ZIP link is not on MedlinePlus"
                    )
                data, _ = collector.fetch(report, url, f"medlineplus-{stamp}.zip")
                records = medline_records(data)
                agency = "U.S. National Library of Medicine · MedlinePlus"
                license_text = (
                    "MedlinePlus XML reuse terms; NLM attribution; third-party links excluded"
                )
                report.notes.append(
                    "Complete bilingual raw XML archived; only selected English title, aliases, "
                    "full-summary and page URL indexed. Generation date is a snapshot date, "
                    "not each topic's medical revision date."
                )
            elif provider == "129-faq":
                records = collector.faq_129(report)
                agency = "보건복지부 · 보건복지상담센터 129"
                license_text = "공공누리 표시 미확인; 무표시 자료 사전협의·공개 RAG 이용허락 필요"
            elif provider == "mohw-129-guidance":
                records = collector.mohw_129_guidance(report)
                agency = "보건복지부 · 보건복지상담센터 129"
                license_text = (
                    "공공누리 제1유형(출처표시), 보건복지부 2025, 게시물 1485141; "
                    "본문 텍스트 정제·분할, 첨부·이미지 제외"
                )
            elif provider == "kdca-older-adults":
                records = collector.kdca_older_adults(report)
                agency = "질병관리청 · 국가건강정보포털"
                license_text = (
                    "공공누리 제4유형: 출처표시·상업적 이용금지·변경금지; 공개 RAG 이용허락 필요"
                )
            elif provider in {"welfare-central", "welfare-local"}:
                records = collector.welfare(report, provider == "welfare-central")
            elif provider == "nhis-search":
                records, nhis_items = collector.nhis_search(report)
                agency = "국민건강보험공단"
            elif provider == "nhis-detail":
                agency = "국민건강보험공단"
                if not nhis_items:
                    raise CollectionError(
                        "blocked_dependency", "No NHIS search IDs available for details"
                    )
                records = collector.nhis_details(report, nhis_items)
            report.records = len(records)
            if records:
                prepared, report.duplicates = document_records(
                    records,
                    provider,
                    agency,
                    license_text,
                )
                report.chunks = len(prepared)
                documents.extend(prepared)
                report.status = "partial" if report.errors else "success"
            elif not report.errors:
                raise CollectionError("empty_corpus", "Provider returned no usable records")
        except CollectionError as error:
            report.errors.append(collector.error(report, error))
        except Exception as error:
            # Never stringify unexpected HTTP/validation exceptions: they may hold signed URLs.
            report.errors.append(
                {
                    "code": "unexpected_error",
                    "message": f"{type(error).__name__}; details withheld",
                }
            )
        if report.errors and report.status != "partial":
            report.status = "partial" if report.chunks else "failed"

    refreshed = {report.provider for report in reports if report.status == "success"}
    new_documents = {document.id: document for document in documents}
    retained = [
        document
        for document in previous
        if document.dataset_id not in refreshed and document.id not in new_documents
    ]
    combined = {document.id: document for document in retained}
    combined.update(new_documents)
    if documents or not output.exists():
        partial = output.with_suffix(".jsonl.partial")
        private_write(
            partial,
            "".join(
                (
                    document.model_dump_json()
                    if document.id in new_documents
                    else previous_lines[document.id]
                )
                + "\n"
                for document in combined.values()
            ).encode("utf-8"),
        )
        partial.replace(output)
    report_path = output_dir / "external-collection-report.json"
    summary: dict[str, Any] = {
        "status": "success"
        if all(r.status == "success" for r in reports)
        else ("partial" if documents else "failed"),
        "collected_at": datetime.now(UTC).isoformat(),
        "records": sum(r.records for r in reports),
        "chunks": len(documents),
        "retained_chunks": len(retained),
        "total_chunks": len(combined),
        "selected_providers": list(selected),
        "output_path": str(output),
        "report_path": str(report_path),
        "raw_dir": str(raw_dir),
        "providers": [vars(report) for report in reports],
    }
    private_write(report_path, (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode())
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw/external"))
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=ALL_PROVIDERS,
        help=(
            "Default: care manual, MedlinePlus and welfare/institution APIs. "
            "Other providers are opt-in; FAQ and KDCA HTML collection remain permission-gated."
        ),
    )
    args = parser.parse_args()
    summary = collect(args.output_dir, raw_dir=args.raw_dir, providers=args.providers)
    public_summary = {
        **summary,
        "providers": [
            {key: value for key, value in provider.items() if key != "audit_requests"}
            for provider in summary["providers"]
        ],
    }
    print(json.dumps(public_summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if summary["status"] == "success" else 1)


if __name__ == "__main__":
    main()
