import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from silvercare import external_sources as sources
from silvercare.config import DataSourceSettings
from silvercare.data import SearchDocument

PDF = b"%PDF-synthetic-welfare-fixture"
NOTICE = """
<div id="contents_body"><article class="board_view">
<h2>2026 나에게 힘이되는 복지서비스</h2>
<ul><li>작성일<span>2026-07-19 16:10</span></li></ul>
<div class="contents"><p>노령층 분책을 제공합니다.</p>
<p>이 책을 활용하여 자체 제작이 가능하나 이미지는 공공의 목적 이외에는
상업용이나 판매용으로 사용할 수 없습니다.</p></div>
</article></div>
""".encode()
STARTS = [
    6,
    7,
    9,
    10,
    11,
    13,
    14,
    15,
    16,
    18,
    19,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    36,
    37,
    38,
    40,
    41,
    42,
    44,
    45,
    46,
]


@dataclass
class Page:
    text: str

    def extract_text(self) -> str:
        return self.text

    def crop(self, box: tuple[int, int, int, int]) -> "Page":
        assert box[0] == 378 and box[2:] == (541, 790)
        return Page("신청방법 및 문의\n공공센터(☎02-123-4567)에서 합성 안내를 확인하세요.")


@pytest.fixture
def guide(monkeypatch: pytest.MonkeyPatch) -> list[Page]:
    pages = [Page("") for _ in range(48)]
    entries = [f"{i:02} 합성복지사업{i} {start}" for i, start in enumerate(STARTS, 1)]
    pages[3].text = "\n".join(entries[:21])
    pages[4].text = "\n".join(entries[21:])
    pages[46].text = "보건복지부\n한국사회보장정보원\n인쇄 2026년 6월 30일\n11-1352000-100665-10"
    for i, start in enumerate(STARTS, 1):
        pages[start - 1].text = (
            f"{i:02}\n합성복지사업{i}\nⅠ대상 합성 이용 대상입니다.\n"
            "Ⅰ내용 합성 지원 내용이며 표 셀 테스트를 포함합니다.\n"
            "Ⅰ방법 합성 기관에서 신청합니다.\n"
            + ("" if i == 6 else "Ⅰ문의 공식센터(☎02-123-4567), 대표번호1355.\n")
            + "자주 하는 질문\nEXCLUDED_FAQ\n"
            f"노령층 {start}"
        )
    monkeypatch.setattr(sources, "WELFARE_SENIOR_SHA256", hashlib.sha256(PDF).hexdigest())
    monkeypatch.setattr(
        sources.pdfplumber, "open", lambda _: nullcontext(type("Guide", (), {"pages": pages})())
    )
    return pages


def test_publisher_permission_requires_article_body_and_exact_edition() -> None:
    assert sources.welfare_senior_permission(NOTICE) == "2026-07-19"
    for invalid in [
        NOTICE.replace(b"2026-07-19", b"2025-07-19"),
        NOTICE.replace(b"contents_body", b"footer"),
        NOTICE.replace(b'class="contents"', b'class="navigation"'),
        NOTICE.replace("자체 제작이 가능".encode(), "제작하려면 협의".encode()),
        NOTICE.replace("노령층".encode(), "청년층".encode()),
    ]:
        with pytest.raises(sources.CollectionError):
            sources.welfare_senior_permission(invalid)


def test_changed_pdf_is_rejected_before_parsing() -> None:
    with pytest.raises(sources.CollectionError, match="changed"):
        sources.welfare_senior_records(PDF, "2026-07-19")


def test_real_parser_malformed_pdf_uses_controlled_collection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = b"%PDF-invalid"
    monkeypatch.setattr(sources, "WELFARE_SENIOR_SHA256", hashlib.sha256(invalid).hexdigest())
    with pytest.raises(sources.CollectionError, match="could not be parsed"):
        sources.welfare_senior_records(invalid, "2026-07-19")


def test_services_preserve_pages_sections_audience_and_disclose_omissions(
    guide: list[Page],
) -> None:
    records = sources.welfare_senior_records(PDF, "2026-07-19")
    assert len(records) == 35
    assert records[1].url.endswith("#page=7")
    assert "원문 쪽 범위: 7–8" in records[1].text
    assert records[21].audience == "provider"
    assert all(record.audience == "self" for i, record in enumerate(records) if i != 21)
    assert all(record.year == "2026" and record.updated == "2026-07-19" for record in records)
    assert all("EXCLUDED_FAQ" not in record.text for record in records)
    assert "합성 지원 내용" not in records[0].text
    assert "표·도식의 세부 조건과 금액은 원문에서 확인" in records[0].text
    assert "합성 지원 내용" in records[2].text
    assert "합성 이용 대상" in records[0].text
    assert "합성 이용 대상" not in records[1].text
    assert "공공센터" in records[4].text
    assert "합성 지원 내용" not in records[4].text


@pytest.mark.parametrize("failure", ["pages", "toc", "imprint", "sections", "date"])
def test_incomplete_edition_or_sections_fail_explicitly(guide: list[Page], failure: str) -> None:
    updated = "2026-07-19"
    if failure == "pages":
        guide.pop()
    elif failure == "toc":
        guide[3].text = guide[3].text.replace("01 합성", "00 합성")
    elif failure == "imprint":
        guide[46].text = "다른 책"
    elif failure == "sections":
        guide[5].text = guide[5].text.replace("Ⅰ방법", "다른 제목")
    else:
        updated = "2026-07-20"
    with pytest.raises(sources.CollectionError):
        sources.welfare_senior_records(PDF, updated)


def test_chunks_repeat_provenance_and_keep_only_publisher_marked_public_landlines(
    guide: list[Page],
) -> None:
    record = sources.welfare_senior_records(PDF, "2026-07-19")[2]
    record = sources.Record(
        record.record_id,
        record.title,
        record.text
        + "\n"
        + "합성 설명을 반복합니다. " * 400
        + "\n개인 휴대전화010-1234-5678, 표시 없는 번호02-345-6789",
        record.url,
        year=record.year,
        updated=record.updated,
        audience=record.audience,
    )
    docs, duplicates = sources.document_records(
        [record], "mohw-welfare-senior-2026", "보건복지부", "합성 이용조건"
    )
    assert duplicates == 0 and len(docs) > 2
    assert all(len(doc.content) <= 1600 for doc in docs)
    assert all(doc.content.startswith("2026 노령층 복지서비스") for doc in docs)
    assert all("인쇄일 2026-06-30" in doc.content for doc in docs)
    assert all("개인별 자격을 확정하지" in doc.content for doc in docs)
    text = "\n".join(doc.content for doc in docs)
    assert "☎02-123-4567" in text
    assert "010-1234-5678" not in text and "02-345-6789" not in text


def test_collecting_guide_retains_unselected_sources(guide: list[Page], tmp_path: Path) -> None:
    legacy = SearchDocument(
        id="legacy",
        title="Existing",
        content="Existing official content",
        source_url="https://example.test/guide",
        dataset_id="existing-provider",
        evidence_kind="reference",
        license="synthetic",
        updated_at="2026-01-01",
    )
    path = tmp_path / "external-knowledge.jsonl"
    original = legacy.model_dump_json()
    path.write_text(original + "\n")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "www.mohw.go.kr":
            return httpx.Response(200, content=NOTICE)
        return httpx.Response(200, content=PDF)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = sources.collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["mohw-welfare-senior-2026"],
        )
    assert len(calls) == 2
    assert report["status"] == "success"
    assert report["records"] == 35
    assert report["retained_chunks"] == 1
    assert original in path.read_text().splitlines()
    assert json.loads((tmp_path / "external-collection-report.json").read_text()) == report
    before = path.read_bytes()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=NOTICE if request.url.host == "www.mohw.go.kr" else PDF + b"changed"
            )
        )
    ) as client:
        failed = sources.collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["mohw-welfare-senior-2026"],
        )
    assert failed["status"] == "failed"
    assert failed["providers"][0]["errors"][0]["code"] == "guide_revision"
    assert path.read_bytes() == before


def test_missing_reuse_notice_does_not_download_pdf_or_overwrite_existing(
    guide: list[Page], tmp_path: Path
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, content=NOTICE.replace("자체 제작이 가능".encode(), "재사용 불가".encode())
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = sources.collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["mohw-welfare-senior-2026"],
        )
    assert len(calls) == 1
    assert report["status"] == "failed"
    assert report["providers"][0]["errors"][0]["code"] == "reuse_permission_required"
