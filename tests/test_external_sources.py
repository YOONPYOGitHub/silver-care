import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
from pydantic import SecretStr

from silvercare.config import DataSourceSettings
from silvercare.external_sources import (
    API_DOCS,
    DEFAULT_PROVIDERS,
    FAQ_129_PAGE,
    KDCA_OLDER_ARTICLES,
    KDCA_VIEW,
    MAX_DOWNLOAD,
    MAX_FAQ_RECORDS,
    MAX_KDCA_SECTIONS,
    MOHW_129_GUIDE,
    PUBLIC_REUSE_NOTICES,
    CollectionError,
    Collector,
    ProviderReport,
    Record,
    api_result,
    collect,
    document_records,
    faq_129_records,
    hwpx_records,
    kdca_records,
    main,
    medline_records,
    mohw_129_records,
    parse_xml,
    zip_members,
)

# Reduced public HTML observed on 2026-11-09; text excerpts are not fabricated advice.
FAQ_HTML = """
<nav><div class="post-con">NAVIGATION ANSWER</div></nav>
<div class="table-responsive-xxl faq-table-wrap"><table class="table"><tbody>
<tr id ="faq18763" class="text-center collapsed faq" aria-expanded="false">
<td data-label="상담분야"><span class="badge w-100 bg-success p-2">노인장기요양제도</span></td>
<td class="text-start table-title faq-title c-pointer" data-label="제목"><p class="m-0">
<a style="color:inherit !important;">장기요양인정서를 분실한 경우 어떻게 다시 받을 수 있나요?</a>
</p></td><td data-label="작성일">2026-11-02</td><td data-label="조회수">185</td></tr>
<tr><td id="faqTd18763" colspan="5" class="hiddenRow">
<div id="faqList18763" class="collapse">
<div class="d-flex align-items-start p-5"><h1 class="fw-bold m-0 me-4">Q</h1>
<p class="m-0">장기요양인정서를 분실한 경우 어떻게 다시 받을 수 있나요?</p></div>
<div class="line border-2 m-0"></div><div class="d-flex align-items-start p-5">
<h1 class="fw-bold m-0 me-4 color">A</h1><div class="w-100"><div class="m-0 post-con">
건강보험공단 장기요양 운영센터로 방문해서 재발급받거나, 유선으로 우편발송 요청
또는 인터넷 등을 통해 재발급 가능합니다.<br />
</div></div></div></div></td></tr>
<tr id ="faq14799" class="text-center collapsed faq" aria-expanded="false">
<td data-label="상담분야"><span class="badge">장애인활동지원</span></td>
<td data-label="제목"><p class="m-0"><a>
장애인활동지원 가족급여 제한적 허용이 가능한 대상자는 누구인가요?</a></p></td>
<td data-label="작성일">2026-11-02</td></tr>
<tr><td id="faqTd14799" colspan="5" class="hiddenRow">
<div id="faqList14799" class="collapse"><div class="d-flex align-items-start p-5">
<h1>Q</h1><p>장애인활동지원 가족급여 제한적 허용이 가능한 대상자는 누구인가요?</p>
</div><div class="d-flex align-items-start p-5"><h1>A</h1><div class="w-100">
<div class="m-0 post-con"><span style="font-family: 맑은 고딕;">
장애인활동지원에 관한 법률 시행령 </span><font face="맑은 고딕">제21조제4호</font>
<span> 개정으로</span><br />
<div data-hjsonver="1.0" id="hwpEditorBoardContent">&nbsp;</div>
</div></div></div></div></td></tr>
</tbody></table></div><footer>FOOTER Copyrights © By MOHW. All Rights Reserved.</footer>
""".encode()


KDCA_HTML = """
<nav><h3>건강정보</h3><div id="tocMenu">NAVIGATION</div></nav>
<div class="hd-info m-dis">
<span><strong>등록일자 :</strong>2020-03-23</span>
<span><strong>업데이트 :</strong>2026-10-23</span>
<span><strong>조회 :</strong>179635</span></div>
<h2><strong id="subContentSj">낙상</strong></h2>
<div class="hd-contents"><div class="low-info"><p>DISCLAIMER</p></div>
<div id="print-content"><div class="s-class">슈퍼클래스</div>
<div id="contentsDiv1" class="contents-Div" style="text-align:left; overflow-x:auto;">
<h3>요약문</h3><div style="padding: 30px;">
<p><span><strong>'이것만은 꼭 기억하세요'</strong></span></p>
<p>• 낙상은 미끄러지거나 걸려 넘어지거나 높은 곳에서 떨어져 다치는 사고를 말합니다.</p>
</div></div><div class="toc-wrap"><a href="#contentsDiv1">TOC</a></div>
<div id="contentsDiv2" class="contents-Div" style="text-align:left; overflow-x:auto;">
<h3>개요-정의</h3>
<p>낙상(落傷)은 여러 가지 원인에 의해서 넘어지거나 떨어져서 몸을 다치는 것으로,</p>
<script>SCRIPT</script><style>STYLE</style></div>
<div id="contentsDiv8" class="contents-Div"><h3>참고문헌</h3><p>REFERENCES</p></div>
</div></div>
<a href="https://www.kogl.or.kr/info/licenseType4.do" target="_blank" title="새창열림"
aria-label="공공누리 제4유형(출처표시+상업적 이용금지+변경금지) 새창열림">
<img src="/healthinfo/static/images/sub/open.png"
alt="공공누리 제4유형(출처표시+상업적 이용금지+변경금지)"></a>
<footer>FOOTER</footer>
""".encode()

MOHW_129_HTML = (
    '<nav>NAVIGATION</nav><div id="contents_body"><article class="board_view">'
    "<h2>보건복지 상담, 수어·채팅·챗봇 전면 개편된 129 앱으로 이용하세요</h2>"
    "<ul><li>작성일: <span>2025-03-31 12:01</span></li></ul>"
    '<div class="viewArea"><p>로그인 없이 웹채팅을 이용하고, '
    "보건복지 챗봇은 365일 24시간 상담하며 누리집에서도 연결합니다.</p>"
    + "<p>합성 테스트의 상담서비스 안내 문단입니다.</p>"
    * 25
    + "<p>&lt;붙임&gt; EXCLUDED ATTACHMENTS</p></div>"
    '<div class="attached">EXCLUDED FILES</div></article>'
    '<div class="codeView01"><img src="/kor/img/nuri/codetype/new_img_opentype01.png" '
    'alt="공공누리 1유형">본 공공저작물은 공공누리 "출처표시" 조건에 따라 이용할 수 있습니다.'
    '</div><div class="prevnext">OTHER POSTS</div></div><footer>FOOTER</footer>'
).encode()


def test_129_licensed_guidance_uses_only_article_body_and_preserves_attribution() -> None:
    records = mohw_129_records(MOHW_129_HTML)
    assert len(records) == 1
    record = records[0]
    assert record.updated == "2025-03-31" and record.year == "2025"
    assert record.url == MOHW_129_GUIDE
    assert record.audience == "self"
    assert "공공누리 제1유형" in record.text
    assert "모든 상담사 상담시간을 뜻하지 않는다" in record.text
    assert not any(
        term in record.text
        for term in [
            "NAVIGATION",
            "EXCLUDED",
            "OTHER POSTS",
            "FOOTER",
        ]
    )


@pytest.mark.parametrize(
    "data",
    [
        MOHW_129_HTML.replace(b"codeView01", b"unrelated"),
        MOHW_129_HTML.replace(b"opentype01", b"opentype04"),
        MOHW_129_HTML.replace("1유형".encode(), "4유형".encode()),
        MOHW_129_HTML.replace(b"</article>", b"</article></div>"),
    ],
)
def test_129_guidance_requires_its_own_type_one_mark(data: bytes) -> None:
    with pytest.raises(CollectionError) as error:
        mohw_129_records(data)
    assert error.value.code == "reuse_permission_required"


def test_129_guidance_rejects_missing_dates_and_article_identity() -> None:
    for data in [
        MOHW_129_HTML.replace(b"2025-03-31", b"2025-02-31"),
        MOHW_129_HTML.replace(b"contents_body", b"footer"),
        MOHW_129_HTML.replace("129 앱".encode(), "다른 앱".encode()),
    ]:
        with pytest.raises(CollectionError) as error:
            mohw_129_records(data)
        assert error.value.code == "article_identity"


def test_collecting_licensed_129_guidance_does_not_clear_faq_answers(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == MOHW_129_GUIDE
        return httpx.Response(200, content=MOHW_129_HTML)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["129-faq", "mohw-129-guidance"],
        )
    assert len(calls) == 1
    assert report["status"] == "partial"
    assert report["providers"][0]["errors"][0]["code"] == "reuse_permission_required"
    assert report["providers"][1]["status"] == "success"
    documents = [
        json.loads(line)
        for line in (tmp_path / "external-knowledge.jsonl").read_text().splitlines()
    ]
    assert documents and all(d["dataset_id"] == "mohw-129-guidance" for d in documents)
    assert all("공공누리 제1유형" in d["license"] for d in documents)


@pytest.fixture
def private_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Test offline collection mechanics separately from the production permission gate.
    monkeypatch.setattr(Collector, "check_public_reuse", lambda self, report: None)


def test_129_observed_markup_pairs_questions_with_own_answers_and_dates() -> None:
    records = faq_129_records(FAQ_HTML)
    assert [record.record_id for record in records] == ["18763", "14799"]
    first, second = records
    assert "Q: 장기요양인정서를 분실한 경우" in first.text
    assert "A: 건강보험공단 장기요양 운영센터" in first.text
    assert "제21조제4호" not in first.text
    assert "A: 장애인활동지원에 관한 법률 시행령 제21조제4호" in second.text
    assert "건강보험공단 장기요양 운영센터" not in second.text
    assert all(record.year == "2026" and record.updated == "2026-11-02" for record in records)
    assert first.url == f"{FAQ_129_PAGE}#faq18763"
    assert "노인장기요양제도" in first.title
    assert all(record.audience == "general" for record in records)
    assert not any(term in first.text for term in ("NAVIGATION", "FOOTER", "조회수", "<span"))


def test_129_chunks_repeat_the_actual_question_context() -> None:
    record = faq_129_records(FAQ_HTML)[0]
    record = replace(record, text=record.text + "\n" + "장기요양인정서 재발급 안내입니다. " * 250)
    documents, _ = document_records([record], "129-faq", "129", "unverified")
    assert len(documents) > 1
    assert all("Q: 장기요양인정서를 분실한 경우" in doc.content for doc in documents)
    assert all("\nA: " in doc.content and len(doc.content) <= 1600 for doc in documents)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"<nav>empty</nav>", "empty_faq"),
        (FAQ_HTML.replace(b"faqList18763", b"faqList99999"), "incomplete_faq"),
        (FAQ_HTML.replace(b"post-con", b"not-answer"), "incomplete_faq"),
        (b"\xff", "invalid_html"),
    ],
)
def test_129_empty_or_unpaired_faq_is_explicit_failure(data: bytes, code: str) -> None:
    with pytest.raises(CollectionError) as error:
        faq_129_records(data)
    assert error.value.code == code


def test_129_selection_is_bounded_even_if_page_grows() -> None:
    pair = (
        '<tr id="faq{number}"><td data-label="제목">실제 질문 {number}</td></tr>'
        '<div id="faqList{number}"><div class="post-con">해당 질문의 답변입니다.</div></div>'
    )
    data = "".join(pair.format(number=number) for number in range(25)).encode()
    assert len(faq_129_records(data)) == MAX_FAQ_RECORDS


def test_kdca_observed_markup_preserves_article_sections_and_publisher_dates() -> None:
    records = kdca_records(KDCA_HTML, "1743", "낙상")
    assert [record.record_id for record in records] == ["1743:contentsDiv1", "1743:contentsDiv2"]
    assert [record.title for record in records] == ["낙상 · 요약문", "낙상 · 개요-정의"]
    assert "떨어져 다치는 사고" in records[0].text
    assert "등록일자: 2020-03-23" in records[0].text
    assert "업데이트: 2026-10-23" in records[0].text
    assert records[0].year == "2020" and records[0].updated == "2026-10-23"
    assert records[0].url == f"{KDCA_VIEW}?cntnts_sn=1743#contentsDiv1"
    joined = "\n".join(record.text for record in records)
    for noise in ("NAVIGATION", "REFERENCES", "FOOTER", "DISCLAIMER", "SCRIPT", "STYLE", "TOC"):
        assert noise not in joined
    documents, _ = document_records(records, "kdca-older-adults", "KDCA", "unverified")
    assert all(doc.audience == "self" for doc in documents)
    assert all(doc.review_status == "automated" for doc in documents)
    assert "검토일" not in joined


def test_public_missing_or_invalid_dates_are_not_replaced_with_crawl_dates() -> None:
    faq = faq_129_records(FAQ_HTML.replace(b"2026-11-02", b"unknown"))
    assert all(not record.updated and not record.year for record in faq)
    kdca = kdca_records(
        KDCA_HTML.replace(b"2020-03-23", b"2020-02-31").replace(b"2026-10-23", b"unknown"),
        "1743",
        "낙상",
    )
    assert all(not record.updated and not record.year for record in kdca)
    assert "등록일자:" not in kdca[0].text and "업데이트:" not in kdca[0].text


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"<nav>blocked</nav>", "article_identity"),
        (KDCA_HTML.replace(b"contents-Div", b"changed-markup"), "empty_article"),
        (KDCA_HTML.replace(b"<h3>", b"<h4>").replace(b"</h3>", b"</h4>"), "incomplete_article"),
    ],
)
def test_kdca_missing_article_content_is_explicit_failure(data: bytes, code: str) -> None:
    with pytest.raises(CollectionError) as error:
        kdca_records(data, "1743", "낙상")
    assert error.value.code == code


def test_kdca_rejects_unselected_articles_and_unbounded_sections() -> None:
    with pytest.raises(CollectionError) as error:
        kdca_records(KDCA_HTML, "99999", "낙상")
    assert error.value.code == "invalid_selection"
    sections = "".join(
        f'<div id="contentsDiv{i}" class="contents-Div"><h3>개요</h3><p>본문</p></div>'
        for i in range(MAX_KDCA_SECTIONS + 1)
    )
    with pytest.raises(CollectionError) as error:
        kdca_records(KDCA_HTML + sections.encode(), "1743", "낙상")
    assert error.value.code == "selection_limit"


def test_selected_public_providers_make_only_five_requests_and_retain_legacy(
    tmp_path: Path,
    private_collection: None,
) -> None:
    calls: list[httpx.Request] = []
    output = tmp_path / "processed"
    output.mkdir()
    legacy, _ = document_records(
        [Record("old", "legacy", "Existing public reference.", "https://example.test/legacy")],
        "medlineplus",
        "agency",
        "terms",
    )
    corpus = output / "external-knowledge.jsonl"
    corpus.write_text(legacy[0].model_dump_json() + "\n")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "serviceKey" not in request.url.params
        if request.url.host == "www.129.go.kr":
            return httpx.Response(200, content=FAQ_HTML)
        assert request.url.host == "health.kdca.go.kr"
        article_id = request.url.params["cntnts_sn"]
        title = dict(KDCA_OLDER_ARTICLES)[article_id]
        return httpx.Response(200, content=KDCA_HTML.replace("낙상".encode(), title.encode()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = collect(
            output,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["129-faq", "kdca-older-adults", "129-faq"],
        )
    assert summary["status"] == "success"
    assert summary["records"] == 10  # Two FAQ pairs and two sections in each of four articles.
    assert len(calls) == 5
    assert summary["selected_providers"] == ["129-faq", "kdca-older-adults"]
    assert summary["retained_chunks"] == 1
    assert summary["total_chunks"] == summary["chunks"] + 1
    saved = [json.loads(line) for line in corpus.read_text().splitlines()]
    assert saved[0] == legacy[0].model_dump()
    assert len(saved) == summary["total_chunks"]
    assert {doc["audience"] for doc in saved if doc["dataset_id"] == "kdca-older-adults"} == {
        "self"
    }


@pytest.mark.parametrize("response", [httpx.Response(403), httpx.Response(200, text="<nav/>")])
def test_failed_selected_refresh_keeps_existing_corpus_byte_for_byte(
    tmp_path: Path,
    response: httpx.Response,
    private_collection: None,
) -> None:
    previous, _ = document_records(faq_129_records(FAQ_HTML)[:1], "129-faq", "129", "terms")
    old = previous[0].model_dump()
    old["dataset_id"] = "129-faq"
    old.pop("audience")  # Also preserve a corpus written before the audience field.
    corpus = tmp_path / "external-knowledge.jsonl"
    original = json.dumps(old, ensure_ascii=False, indent=None).encode() + b"\n"
    corpus.write_bytes(original)
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        summary = collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["129-faq"],
        )
    assert summary["status"] == "failed"
    assert summary["records"] == summary["chunks"] == 0
    assert summary["retained_chunks"] == summary["total_chunks"] == 1
    assert corpus.read_bytes() == original
    assert summary["providers"][0]["errors"][0]["code"] in {"http_403", "empty_faq"}


def test_kdca_article_failure_is_reported_without_discarding_successes(
    tmp_path: Path,
    private_collection: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        article_id = request.url.params["cntnts_sn"]
        if article_id == "1743":
            return httpx.Response(200, content=KDCA_HTML)
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["kdca-older-adults"],
        )
    report = summary["providers"][0]
    assert summary["status"] == report["status"] == "partial"
    assert report["requests"] == 4 and report["records"] == 2
    assert len(report["errors"]) == 3


def test_failed_faq_is_retained_verbatim_when_other_selected_provider_succeeds(
    tmp_path: Path,
    private_collection: None,
) -> None:
    previous, _ = document_records(faq_129_records(FAQ_HTML)[:1], "129-faq", "129", "terms")
    old = previous[0].model_dump()
    old.pop("audience")
    original_line = json.dumps(old, ensure_ascii=False) + "\n"
    corpus = tmp_path / "external-knowledge.jsonl"
    corpus.write_text(original_line)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.129.go.kr":
            return httpx.Response(403)
        title = dict(KDCA_OLDER_ARTICLES)[request.url.params["cntnts_sn"]]
        return httpx.Response(200, content=KDCA_HTML.replace("낙상".encode(), title.encode()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["129-faq", "kdca-older-adults"],
        )
    assert summary["status"] == "partial"
    assert corpus.read_text().startswith(original_line)
    assert summary["retained_chunks"] == 1
    assert summary["total_chunks"] == summary["chunks"] + 1


def test_public_transport_failures_are_reported_with_no_retries(
    tmp_path: Path,
    private_collection: None,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("simulated timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=["129-faq", "kdca-older-adults"],
        )
    assert summary["status"] == "failed"
    assert len(calls) == 5
    assert all(
        error["code"] == "transport_error"
        for report in summary["providers"]
        for error in report["errors"]
    )


@pytest.mark.parametrize("provider", ["129-faq", "kdca-older-adults"])
def test_uncleared_public_reuse_reports_required_permission_without_fetching(
    provider: str,
    tmp_path: Path,
) -> None:
    records = faq_129_records(FAQ_HTML)[:1]
    previous, _ = document_records(records, provider, "agency", "uncleared")
    original = previous[0].model_dump_json() + "\n"
    corpus = tmp_path / "external-knowledge.jsonl"
    corpus.write_text(original)

    def reject_network(request: httpx.Request) -> httpx.Response:
        pytest.fail("No article download is allowed before reuse clearance")

    with httpx.Client(transport=httpx.MockTransport(reject_network)) as client:
        summary = collect(
            tmp_path,
            settings=DataSourceSettings(_env_file=None),
            client=client,
            raw_dir=tmp_path / "raw",
            providers=[provider],
        )
    report = summary["providers"][0]
    error = report["errors"][0]
    assert summary["status"] == report["status"] == "failed"
    assert summary["records"] == summary["chunks"] == report["requests"] == 0
    assert report["raw_files"] == []
    assert error["code"] == "reuse_permission_required"
    assert error["terms_url"] == PUBLIC_REUSE_NOTICES[provider]["terms_url"]
    assert "Retained copies are not cleared" in error["action"]
    assert corpus.read_text() == original
    if provider == "kdca-older-adults":
        assert error["license_url"] == "https://www.kogl.or.kr/info/licenseType4.do"
        assert error["application_url"].endswith("hlthinsReqstMth.do?index=3")
        assert "API approval alone" in error["action"]
    else:
        assert error["source_notice_url"] == FAQ_129_PAGE
        assert "prior consultation" in error["action"]
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize("provider", ["129-faq", "kdca-older-adults"])
def test_direct_public_collector_calls_do_not_bypass_permission(
    provider: str,
    tmp_path: Path,
) -> None:
    with httpx.Client() as client:
        collector = Collector(client, DataSourceSettings(_env_file=None), tmp_path)
        report = ProviderReport(provider)
        with pytest.raises(CollectionError) as error:
            if provider == "129-faq":
                collector.faq_129(report)
            else:
                collector.kdca_older_adults(report)
    assert error.value.code == "reuse_permission_required"
    assert report.requests == 0


def test_invalid_existing_corpus_is_not_overwritten_or_requested(tmp_path: Path) -> None:
    corpus = tmp_path / "external-knowledge.jsonl"
    corpus.write_text("invalid json\n")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("No network expected"))
    ) as c:
        with pytest.raises(CollectionError, match="refusing to overwrite"):
            collect(
                tmp_path,
                settings=DataSourceSettings(_env_file=None),
                client=c,
                providers=["129-faq"],
            )
    assert corpus.read_text() == "invalid json\n"


@pytest.mark.parametrize("providers", [[], ["unknown"]])
def test_invalid_provider_selection_is_rejected(providers: list[str], tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="known"):
        collect(tmp_path, providers=providers)


def test_cli_supports_explicit_public_provider_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {}

    def fake_collect(output_dir: Path, **kwargs: object) -> dict[str, object]:
        called.update({"output_dir": output_dir, **kwargs})
        return {"status": "success", "providers": []}

    monkeypatch.setattr("silvercare.external_sources.collect", fake_collect)
    monkeypatch.setattr(
        "sys.argv",
        [
            "collector",
            "--providers",
            "129-faq",
            "kdca-older-adults",
            "--output-dir",
            str(tmp_path / "processed"),
            "--raw-dir",
            str(tmp_path / "raw"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
    assert called["providers"] == ["129-faq", "kdca-older-adults"]
    assert called["raw_dir"] == tmp_path / "raw"
    assert not {"129-faq", "kdca-older-adults"}.intersection(DEFAULT_PROVIDERS)


def archive(members: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        for name, content in members.items():
            handle.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("status", [200, 403])
def test_internal_gateway_error_on_any_http_status(status: int) -> None:
    data = b"""<OpenAPI_ServiceResponse><cmmMsgHeader>
    <errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>
    <returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""
    with pytest.raises(CollectionError) as error:
        api_result(data, status)
    assert error.value.code == "30"
    assert error.value.message == "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"


@pytest.mark.parametrize("code", ["20", "22", "31", "99"])
def test_welfare_application_error(code: str) -> None:
    with pytest.raises(CollectionError) as error:
        api_result(f"<wantedList><resultCode>{code}</resultCode></wantedList>".encode(), 200)
    assert error.value.code == code


@pytest.mark.parametrize("code", ["0", "00", "0000"])
def test_success_envelope(code: str) -> None:
    _, result, _ = api_result(
        f"<response><header><resultCode>{code}</resultCode></header></response>".encode(),
        200,
    )
    assert result == code


def test_missing_status_is_not_success() -> None:
    with pytest.raises(CollectionError, match="status envelope"):
        api_result(b"<response/>", 200)


def test_entity_declarations_rejected() -> None:
    with pytest.raises(CollectionError, match="entities"):
        parse_xml(b'<!DOCTYPE r [<!ENTITY x "x">]><r>&x;</r>')


def test_keys_decoded_once_and_errors_do_not_leak(tmp_path: Path) -> None:
    key = "a%2Bb%2Fc%3D"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["serviceKey"] == "a+b/c="
        return httpx.Response(
            200,
            text=(
                "<r><resultCode>30</resultCode>"
                f"<resultMessage>{key} a+b/c= https://example.test/?serviceKey={key}"
                "</resultMessage></r>"
            ),
        )

    settings = DataSourceSettings(_env_file=None, welfare_api_key=SecretStr(key))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = Collector(client, settings, tmp_path / "raw")
        report = ProviderReport("welfare-local")
        with pytest.raises(CollectionError) as error:
            collector.api(report, "https://example.test/list", "response.xml", key, {})
        safe = collector.error(report, error.value)
    text = json.dumps([safe, report.results])
    assert key not in text
    assert "a+b/c=" not in text
    assert "serviceKey=" not in text
    assert API_DOCS["welfare-local"] in text
    assert (tmp_path / "raw/response.xml").stat().st_mode & 0o777 == 0o600


def test_transport_error_never_prints_signed_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(str(request.url), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = Collector(client, DataSourceSettings(_env_file=None), tmp_path)
        with pytest.raises(CollectionError) as error:
            collector.api(
                ProviderReport("welfare-local"),
                "https://example.test/",
                "r.xml",
                "SECRET",
                {},
            )
    assert "SECRET" not in str(error.value)
    assert "serviceKey" not in str(error.value)


def test_decoded_key_encoded_echo_is_hidden(tmp_path: Path) -> None:
    settings = DataSourceSettings(_env_file=None, welfare_api_key=SecretStr("a+b/c="))
    with httpx.Client() as client:
        collector = Collector(client, settings, tmp_path)
        assert "a%2Bb%2Fc%3D" not in collector.safe_message("error a%2Bb%2Fc%3D")


def test_signed_requests_do_not_follow_redirects(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.example.test"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        collector = Collector(client, DataSourceSettings(_env_file=None), tmp_path)
        with pytest.raises(CollectionError):
            collector.api(
                ProviderReport("welfare-local"),
                "https://example.test",
                "r.xml",
                "SECRET",
                {},
            )
    assert len(calls) == 1


def test_download_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("silvercare.external_sources.MAX_DOWNLOAD", 16)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 17)),
    ) as client:
        collector = Collector(client, DataSourceSettings(_env_file=None), tmp_path)
        with pytest.raises(CollectionError, match="download limit"):
            collector.fetch(ProviderReport("test"), "https://example.test", "r.xml")
    assert not (tmp_path / "r.xml").exists()


def test_zip_member_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    data = archive({"large.xml": "x" * 40})
    monkeypatch.setattr("silvercare.external_sources.MAX_DOWNLOAD", 16)
    with pytest.raises(CollectionError, match="safety limits"):
        list(zip_members(data))
    assert MAX_DOWNLOAD > 16


def test_medline_full_summary_aliases_english_dedup() -> None:
    summary = escape("<p>Care for older adults.</p><ul><li>Keep active.</li></ul>")
    topic = (
        '<health-topic id="11" title="Healthy Aging" language="English" '
        'url="https://medlineplus.gov/healthyaging.html">'
        f"<also-called>Successful aging</also-called><full-summary>{summary}</full-summary>"
        "</health-topic>"
    )
    xml = (
        '<health-topics date-generated="11/07/2026 02:30:37">'
        + topic
        + topic
        + topic.replace('language="English"', 'language="Spanish"').replace('id="11"', 'id="12"')
        + topic.replace('title="Healthy Aging"', 'title="Child Nutrition"')
        + "</health-topics>"
    )
    records = medline_records(archive({"mplus_topics_2026-11-07.xml": xml}))
    assert len(records) == 1
    assert records[0].record_id == "11"
    assert "Also called: Successful aging" in records[0].text
    assert "Keep active." in records[0].text
    assert "<p>" not in records[0].text
    assert records[0].updated == "2026-11-07"


def test_medline_empty_is_failure() -> None:
    with pytest.raises(CollectionError, match="No selected"):
        medline_records(
            archive(
                {
                    "mplus_topics_2026-11-07.xml": '<health-topics date-generated="11/07/2026"/>',
                }
            )
        )


def test_medline_missing_date_is_explicit_failure() -> None:
    with pytest.raises(CollectionError, match="generation date"):
        medline_records(archive({"mplus_topics_2026-11-07.xml": "<health-topics/>"}))


def hwpx() -> bytes:
    members = {}
    for number in range(9):
        body = (
            '<p styleIDRef="6"><run><t>가. 안전지원 서비스</t></run></p>'
            "<p><run><t>정기적인 안부 확인과 안전 점검을 제공합니다.</t></run></p>"
            "<p><run><tbl>"
            "<tr><tc><p><run><t>서비스 종류</t></run></p></tc>"
            "<tc><p><run><t>서비스 내용</t></run></p></tc></tr>"
            "<tr><tc><p><run><t>안부 확인</t></run></p></tc>"
            "<tc><p><run><t>정기적으로 방문하여 확인합니다.</t></run></p></tc></tr>"
            "</tbl></run></p>"
        )
        if number in {0, 7, 8}:
            body += "<p><run><t>EXCLUDE_FORM_AND_TOC</t></run></p>"
        members[f"Contents/section{number}.xml"] = "<sec>" + body + "</sec>"
    return archive(members)


def test_hwpx_preserves_paragraph_table_context_excludes_forms() -> None:
    records = hwpx_records(hwpx())
    text = "\n".join(record.text for record in records)
    assert "정기적인 안부 확인과 안전 점검을 제공합니다." in text
    assert "표 머리행: 서비스 종류 | 서비스 내용" in text
    assert "안부 확인 | 정기적으로 방문하여 확인합니다." in text
    assert "가. 안전지원 서비스" in text
    assert "EXCLUDE_FORM_AND_TOC" not in text
    assert all(record.year == "2026" for record in records)
    assert [r.record_id for r in records] == [r.record_id for r in hwpx_records(hwpx())]


def test_document_dedup_stable_ids_and_contract() -> None:
    one = Record("a", "title", "A meaningful factual paragraph. " * 120, "https://example.test/a")
    two = Record("b", "title", one.text, "https://example.test/b")
    documents, duplicates = document_records([one, two], "source", "agency", "terms")
    assert duplicates == 1
    again, _ = document_records([one], "source", "agency", "terms")
    assert [d.id for d in documents] == [d.id for d in again]
    assert len(documents) > 1
    assert all(d.evidence_kind == "reference" and d.review_status == "automated" for d in documents)
    assert all(d.source_year == "" for d in documents)
    assert all(len(d.content) <= 1600 for d in documents)


@pytest.mark.parametrize("detail_code", ["0", "30"])
def test_welfare_detail_budget_and_partial_failure(tmp_path: Path, detail_code: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "www.data.go.kr":
            return httpx.Response(200, text="official docs")
        if request.url.path.endswith("/NationalWelfarelistV001"):
            assert request.url.params["age"] == "70"
            assert request.url.params["searchWrd"] == "노인"
            items = "".join(
                f"<servList><servId>{i}</servId><servNm>노인 서비스 {i}</servNm>"
                "<servDgst>어르신을 위한 생활지원 서비스를 제공합니다.</servDgst></servList>"
                for i in range(20)
            )
            return httpx.Response(200, text=f"<r><resultCode>0</resultCode>{items}</r>")
        return httpx.Response(
            200,
            text=(
                f"<r><resultCode>{detail_code}</resultCode>"
                "<resultMessage>RESULT</resultMessage>"
                "<wlfareInfoOutlCn>어르신을 위한 생활지원 서비스를 제공합니다."
                "</wlfareInfoOutlCn></r>"
            ),
        )

    settings = DataSourceSettings(_env_file=None, welfare_api_key=SecretStr("SECRET"))
    report = ProviderReport("welfare-central")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = Collector(client, settings, tmp_path).welfare(report, central=True)
    assert len(records) == 20
    assert report.detail_calls == (1 if detail_code == "30" else 10)
    assert len(calls) == (3 if detail_code == "30" else 12)
    assert len(report.errors) == (1 if detail_code == "30" else 0)
    assert all("serviceKey" not in record.url and "SECRET" not in record.url for record in records)
    assert all(
        record.url == "https://www.data.go.kr/data/15090532/openapi.do" for record in records
    )
    assert all("serviceKey" not in entry["unsigned_url"] for entry in report.audit_requests)


@pytest.mark.parametrize("provider", ["nhis-search", "nhis-detail"])
def test_nhis_public_urls_and_private_query_audit(tmp_path: Path, provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.data.go.kr":
            return httpx.Response(200, text="official docs")
        return httpx.Response(
            200,
            text=(
                "<response><header><resultCode>00</resultCode></header><body><items><item>"
                "<longTermAdminSym>11111111111</longTermAdminSym>"
                "<adminNm>노인돌봄센터</adminNm></item></items></body></response>"
            ),
        )

    report = ProviderReport(provider)
    settings = DataSourceSettings(_env_file=None, nhis_ltc_api_key=SecretStr("SECRET"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        collector = Collector(client, settings, tmp_path)
        if provider == "nhis-search":
            records, _ = collector.nhis_search(report)
        else:
            item = parse_xml(b"<item><longTermAdminSym>11111111111</longTermAdminSym></item>")
            records = collector.nhis_details(report, [item])
    assert len(records) == 1
    assert records[0].url == f"https://www.data.go.kr/data/{API_DOCS[provider]}/openapi.do"
    audit_url = report.audit_requests[0]["unsigned_url"]
    assert audit_url.startswith("https://apis.data.go.kr/")
    assert "serviceKey" not in audit_url and "SECRET" not in audit_url
    assert report.audit_requests[0]["raw_file"].endswith(".xml")
    documents, _ = document_records(records, provider, "agency", "terms")
    previous = Record(
        records[0].record_id,
        records[0].title,
        records[0].text,
        audit_url,
    )
    previous_documents, _ = document_records([previous], provider, "agency", "terms")
    assert documents[0].id == previous_documents[0].id
    assert documents[0].source_record_id == previous_documents[0].source_record_id


@pytest.mark.parametrize(
    ("source_url", "expected_url"),
    [
        (
            "https://apis.data.go.kr/B554287/service?servId=1",
            "https://www.data.go.kr/data/15090532/openapi.do",
        ),
        ("https://www.bokjiro.go.kr/public-service", "https://www.bokjiro.go.kr/public-service"),
    ],
)
def test_welfare_api_links_replaced_but_public_pages_preserved(
    tmp_path: Path,
    source_url: str,
    expected_url: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.data.go.kr":
            return httpx.Response(200, text="official docs")
        return httpx.Response(
            200,
            text=(
                "<r><resultCode>0</resultCode><servList><servId>1</servId>"
                "<servNm>노인 서비스</servNm><servDgst>어르신을 위한 생활지원 서비스</servDgst>"
                f"<servDtlLink>{escape(source_url)}</servDtlLink></servList></r>"
            ),
        )

    settings = DataSourceSettings(_env_file=None, welfare_api_key=SecretStr("SECRET"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = Collector(client, settings, tmp_path).welfare(
            ProviderReport("welfare-central"),
            central=True,
        )
    assert records[0].url == expected_url


def test_nhis_detail_uses_current_official_operation_and_stops_on_denial(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "www.data.go.kr":
            return httpx.Response(200, text="official docs")
        assert request.url.path.endswith(
            "/getLtcInsttDetailInfoService02/getConvInsttDetailInfoList02",
        )
        assert request.url.params["longTermAdminSym"] == "11111111111"
        return httpx.Response(
            403,
            text="<r><returnReasonCode>30</returnReasonCode><errMsg>UNREGISTERED</errMsg></r>",
        )

    items = [
        parse_xml(b"<item><longTermAdminSym>11111111111</longTermAdminSym></item>"),
        parse_xml(b"<item><longTermAdminSym>22222222222</longTermAdminSym></item>"),
    ]
    report = ProviderReport("nhis-detail")
    settings = DataSourceSettings(_env_file=None, nhis_ltc_api_key=SecretStr("SECRET"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        records = Collector(client, settings, tmp_path).nhis_details(report, items)
    assert records == []
    assert report.detail_calls == 1
    assert len(calls) == 2
    assert report.errors[0]["code"] == "30"


def test_partial_collection_persists_failures_and_successes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.mohw.go.kr":
            if request.url.path == "/boardDownload.es":
                return httpx.Response(200, content=hwpx())
            return httpx.Response(
                200,
                text="2026.7.27 작성일</strong><span>2026-09-02 15:15</span>",
            )
        if request.url.host == "www.data.go.kr":
            return httpx.Response(200, text="<html>Official docs</html>")
        return httpx.Response(
            403,
            text="<r><returnReasonCode>30</returnReasonCode><errMsg>UNREGISTERED</errMsg></r>",
        )

    settings = DataSourceSettings(
        _env_file=None,
        welfare_api_key=SecretStr("SECRET"),
        nhis_ltc_api_key=SecretStr("SECRET"),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = collect(
            tmp_path / "processed", settings=settings, client=client, raw_dir=tmp_path / "raw"
        )
    assert summary["status"] == "partial"
    assert summary["chunks"] > 0
    providers = {report["provider"]: report for report in summary["providers"]}
    assert providers["mohw-care-2026"]["status"] == "success"
    assert providers["welfare-local"]["errors"][0]["code"] == "30"
    assert providers["nhis-detail"]["errors"][0]["code"] == "blocked_dependency"
    assert "SECRET" not in json.dumps(summary)
    assert Path(summary["output_path"]).exists()
    assert json.loads(Path(summary["report_path"]).read_text())["status"] == "partial"


def test_each_public_api_uses_its_own_dedicated_key(tmp_path, monkeypatch):
    from xml.etree.ElementTree import fromstring

    settings = DataSourceSettings(
        _env_file=None,
        welfare_central_api_key=SecretStr("central-only"),
        welfare_local_api_key=SecretStr("local-only"),
        nhis_ltc_search_api_key=SecretStr("search-only"),
        nhis_ltc_detail_api_key=SecretStr("detail-only"),
    )
    calls = []

    def stop_after_key(report, url, filename, key, params):
        calls.append((report.provider, key))
        raise CollectionError("30", "synthetic stop after credential selection")

    with httpx.Client() as client:
        collector = Collector(client, settings, tmp_path)
        monkeypatch.setattr(collector, "official_docs", lambda report: "https://example.test/docs")
        monkeypatch.setattr(collector, "api", stop_after_key)
        for central, provider in ((True, "welfare-central"), (False, "welfare-local")):
            with pytest.raises(CollectionError):
                collector.welfare(ProviderReport(provider), central)
        with pytest.raises(CollectionError):
            collector.nhis_search(ProviderReport("nhis-search"))
        collector.nhis_details(
            ProviderReport("nhis-detail"),
            [fromstring("<item><longTermAdminSym>test-id</longTermAdminSym></item>")],
        )
        for secret in ("central-only", "local-only", "search-only", "detail-only"):
            assert secret not in collector.safe_message("error " + secret)
    assert calls == [
        ("welfare-central", "central-only"),
        ("welfare-local", "local-only"),
        ("nhis-search", "search-only"),
        ("nhis-detail", "detail-only"),
    ]
