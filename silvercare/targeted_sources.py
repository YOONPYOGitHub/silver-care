"""Collect scoped, licensed adult emergency and supplement references."""

import argparse
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx

from silvercare.config import DataSourceSettings
from silvercare.data import chunks
from silvercare.external_sources import (
    CollectionError,
    Collector,
    ProviderReport,
    Record,
    SourceElement,
    SourceHTML,
    document_records,
    private_write,
)

BLS_URL = "https://www.ceemjournal.org/journal/view.php?doi=10.15441/ceem.26.095"
BLS_LICENSE = "https://creativecommons.org/licenses/by-nc/4.0/"
NCCIH_POLICY = "https://www.nccih.nih.gov/tools/privacy"
NEI_POLICY = "https://www.nei.nih.gov/web-policies/copyright-policy"
CDC_POLICY = "https://www.cdc.gov/other/agencymaterials.html"
MEDLINE_POLICY = "https://medlineplus.gov/about/using/usingcontent/"
AGENCIES = {
    "nccih-supplements": "NIH NCCIH",
    "nei-areds2": "NIH National Eye Institute",
    "cdc-diabetes": "U.S. Centers for Disease Control and Prevention",
    "medlineplus-injury": "MedlinePlus, National Library of Medicine",
}
POLICIES = {
    "nccih-supplements": NCCIH_POLICY,
    "nei-areds2": NEI_POLICY,
    "cdc-diabetes": CDC_POLICY,
    "medlineplus-injury": MEDLINE_POLICY,
}
PAGES = (
    (
        "cdc-diabetes",
        "hypoglycemia",
        "성인 저혈당 · 15 g 탄수화물 · 15분 재측정 · 의식·삼킴 조건",
        "https://www.cdc.gov/diabetes/treatment/treatment-low-blood-sugar-hypoglycemia.html",
        "Treatment of Low Blood Sugar (Hypoglycemia)",
        (
            "Key points",
            "The 15-15 rule",
            "Foods to treat low blood sugar",
            "Treating severely low blood sugar",
            "Prevention",
            "Things for loved ones to know",
        ),
    ),
    (
        "medlineplus-injury",
        "concussion",
        "머리 부상·뇌진탕 · 늦게 나타나는 증상·반복 구토·깨우기 어려움",
        "https://medlineplus.gov/concussion.html",
        "Concussion",
        ("Summary",),
    ),
    (
        "nccih-supplements",
        "glucosamine",
        "글루코사민·콘드로이틴·무릎 골관절염",
        "https://www.nccih.nih.gov/health/"
        "glucosamine-and-chondroitin-for-osteoarthritis-what-you-need-to-know",
        "Glucosamine and Chondroitin for Osteoarthritis: What You Need To Know",
        (
            "What are glucosamine and chondroitin?",
            "Are glucosamine and chondroitin helpful for symptoms of knee osteoarthritis?",
            "Do glucosamine and chondroitin have effects on joint structure "
            "in knee osteoarthritis?",
            "Are glucosamine and chondroitin helpful for osteoarthritis in other joints?",
            "Are glucosamine and chondroitin safe?",
        ),
    ),
    (
        "nccih-supplements",
        "probiotics",
        "유산균·프로바이오틱스·변비·균주별 효과와 안전성",
        "https://www.nccih.nih.gov/health/probiotics-usefulness-and-safety",
        "Probiotics: Usefulness and Safety",
        (
            "What are probiotics?",
            "What types of bacteria are in probiotics?",
            "Constipation",
            "Can probiotics be harmful?",
            "More To Consider",
        ),
    ),
    (
        "nei-areds2",
        "areds2",
        "루테인·지아잔틴·AREDS2·황반변성·백내장",
        "https://www.nei.nih.gov/eye-health-information/clinical-trials/"
        "age-related-eye-disease-studies-aredsareds2/aredsareds2-frequently-asked-questions",
        "AREDS/AREDS2 Frequently Asked Questions",
        (
            "Are the AREDS vitamins right for me?",
            "Will taking the AREDS or AREDS2 supplements prevent AMD?",
            "Why are the AREDS and AREDS2 formulas different?",
            "What are lutein, zeaxanthin, and beta-carotene?",
            "Do the AREDS/AREDS2 formulations interfere with other medications?",
            "Will the AREDS/AREDS2 formulations help prevent cataract?",
        ),
    ),
)


def blocks(element: SourceElement) -> Iterator[tuple[str, str]]:
    for child in element.children:
        if not isinstance(child, SourceElement):
            continue
        if child.tag in {"h1", "h2", "h3", "h4", "p", "ul", "ol"}:
            yield child.tag, child.text()
        elif child.tag not in {"script", "style", "nav", "footer", "figure", "table"}:
            yield from blocks(child)


def selected_sections(
    data: bytes, expected_title: str, headings: tuple[str, ...]
) -> dict[str, str]:
    root = SourceHTML(data).root
    mains = [node for node in root.elements() if node.tag == "main"]
    if not mains:
        mains = [node for node in root.elements() if node.tag == "article"]
    if len(mains) != 1:
        raise CollectionError("article_identity", "Expected one medical article main element")
    titles = [node.text() for node in root.elements() if node.tag == "h1"]
    if titles != [expected_title]:
        raise CollectionError("article_identity", "Medical article title changed")
    sections: dict[str, list[str]] = {}
    current = ""
    for tag, text in blocks(mains[0]):
        if tag in {"h1", "h2", "h3", "h4"} and text != "More":
            current = text
            if text in headings:
                if text in sections:
                    raise CollectionError("article_identity", "Duplicate selected heading")
                sections[text] = []
        elif current in sections and tag in {"p", "ul", "ol"} and text:
            sections[current].append(text)
    if set(sections) != set(headings) or any(
        len("\n".join(parts)) < 80 for parts in sections.values()
    ):
        raise CollectionError("incomplete_evidence", "A selected medical section is missing")
    return {heading: "\n".join(sections[heading]) for heading in headings}


def airway_section(data: bytes) -> str:
    root = SourceHTML(data).root
    licenses = [
        node.attrs.get("href", "").replace("http://", "https://")
        for node in root.elements()
        if node.tag == "a"
    ]
    if BLS_LICENSE not in licenses or "2025 Korean Guidelines" not in root.text():
        raise CollectionError("reuse_permission_required", "Adult BLS identity/license changed")
    for parent in root.elements():
        children = [child for child in parent.children if isinstance(child, SourceElement)]
        starts = [i for i, child in enumerate(children) if child.attrs.get("id") == "sec07"]
        ends = [i for i, child in enumerate(children) if child.attrs.get("id") == "sec08"]
        if len(starts) == len(ends) == 1 and starts[0] < ends[0]:
            section = "\n".join(
                child.text()
                for child in children[starts[0] : ends[0]]
                if child.tag not in {"figure", "table", "script", "style"}
            )
            required = (
                "FOREIGN BODY AIRWAY OBSTRUCTION",
                "unable to cough effectively",
                "five",
                "back blows",
                "abdominal thrusts",
                "unresponsive",
            )
            if not all(phrase in section for phrase in required):
                raise CollectionError("incomplete_evidence", "Adult airway conditions are missing")
            return section
    raise CollectionError("article_identity", "Adult BLS airway section boundaries changed")


def scoped_records(records: list[Record], prefix: str) -> list[Record]:
    result = []
    for record in records:
        context = prefix + record.title + "\n"
        for i, part in enumerate(chunks(record.text, size=1600 - len(context), overlap=120)):
            result.append(
                Record(
                    f"{record.record_id}:chunk-{i}",
                    record.title,
                    context + part,
                    record.url,
                    record.year,
                    record.updated,
                    record.audience,
                )
            )
    return result


def collect(output: Path, raw: Path, *, noncommercial: bool) -> dict[str, object]:
    if not noncommercial:
        raise CollectionError("reuse_permission_required", "Adult BLS requires noncommercial use")
    documents = []
    reports = []
    with httpx.Client(timeout=60) as client:
        collector = Collector(client, DataSourceSettings(), raw)
        policy_report = ProviderReport("targeted-reuse-policies")
        for url, filename, phrases in (
            (NCCIH_POLICY, "nccih-policy.html", ("not copyrighted", "public domain")),
            (NEI_POLICY, "nei-policy.html", ("public domain", "freely copy")),
            (
                CDC_POLICY,
                "cdc-policy.html",
                ("public domain", "may not change the substantive content", "no charge"),
            ),
            (
                MEDLINE_POLICY,
                "medline-policy.html",
                ("Summaries on health topic pages", "public domain"),
            ),
        ):
            data, _ = collector.fetch(policy_report, url, filename)
            text = SourceHTML(data).root.text()
            if not all(phrase in text for phrase in phrases):
                raise CollectionError("reuse_permission_required", "NIH text reuse terms changed")
        policy_report.status = "success"
        reports.append(vars(policy_report))
        for dataset, identity, topic, url, title, headings in PAGES:
            report = ProviderReport(dataset)
            data, _ = collector.fetch(report, url, f"{identity}.html")
            sections = selected_sections(data, title, headings)
            agency = AGENCIES[dataset]
            prefix = (
                f"{agency} 환자교육 원문 발췌 · {topic}\n"
                "영문 원문의 선택한 절만 수록. "
                "개인별 처방이나 한국 제품의 허가·효능 인증이 아닙니다. "
                "연구 대상·제형·균주·질환 단계별 조건과 불확실성을 보존하세요. "
                "일반 영양제나 다른 질환·제품으로 효과를 옮겨 적용하지 마세요.\n"
            )
            records = [
                Record(
                    f"{identity}:{i}",
                    f"{topic} · {heading}",
                    title + "\n" + heading + "\n" + text,
                    url,
                    audience="self",
                )
                for i, (heading, text) in enumerate(sections.items())
            ]
            license_text = (
                f"Public-domain selected text; source {agency}; {POLICIES[dataset]}; "
                "no images or linked third-party works; no endorsement"
            )
            if dataset == "cdc-diabetes":
                license_text += (
                    "; original substantive content/conditions unchanged; "
                    "original available free on CDC website; "
                    "CDC/HHS/U.S. Government does not endorse this service"
                )
            prepared, duplicates = document_records(
                scoped_records(records, prefix),
                dataset,
                agency,
                license_text,
            )
            documents.extend(prepared)
            report.status, report.records, report.chunks = "success", len(records), len(prepared)
            report.duplicates = duplicates
            report.notes.append(
                "Source date not reliably machine-readable; left blank, "
                "not replaced by crawl date. "
                "Original selected sections retained without a new clinical review."
            )
            reports.append(vars(report))
        report = ProviderReport("korean-bls-2025")
        data, _ = collector.fetch(report, BLS_URL, "korean-bls-2025.html")
        text = airway_section(data)
        prefix = (
            "2025 한국 심폐소생술 지침 · 성인 기본소생술 · 이물 기도폐쇄\n"
            "2026년 출판 영문 지침의 일반인 구조자용 기도폐쇄 절만 발췌·정제. "
            "원문 저작권자: The Korean Society of Emergency Medicine. "
            "CC BY-NC 4.0, 비상업적 이용. 원 저자의 한국어 안내나 서비스 보증이 아닙니다. "
            "성인과 1세 이상 소아가 함께 언급된 원문이며 영아 절차는 포함하지 않습니다. "
            "의식·효과적인 기침 여부·처치 순서·의식 소실 시 중단 기준을 분리하지 마세요. "
            "실제 응급상황에서는 119의 현장 지시를 우선합니다.\n"
        )
        prepared, _ = document_records(
            scoped_records(
                [
                    Record(
                        "ceem-26-095:airway",
                        "성인 기도폐쇄 · 2025 한국 기본소생술 지침",
                        text,
                        BLS_URL + "#sec07",
                        "2026",
                        audience="caregiver",
                    )
                ],
                prefix,
            ),
            "korean-bls-2025",
            "2025 Korean BLS Guidelines · Korean Society of Emergency Medicine",
            f"CC BY-NC 4.0; {BLS_LICENSE}; selected text adaptation; "
            "Korean Society of Emergency Medicine, 2026; DOI 10.15441/ceem.26.095",
        )
        for document in prepared:
            document.risk_level = "clinical"
        documents.extend(prepared)
        report.status, report.records, report.chunks = "success", 1, len(prepared)
        reports.append(vars(report))
    payload = "".join(document.model_dump_json() + "\n" for document in documents).encode()
    summary = {
        "status": "success",
        "collected_at": datetime.now(UTC).isoformat(),
        "clinical_review_status": "not_performed",
        "documents": len(documents),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "providers": reports,
        "raw_sha256": {
            filename: hashlib.sha256(Path(filename).read_bytes()).hexdigest()
            for report in reports
            for filename in report["raw_files"]
        },
    }
    private_write(output / "targeted-medical-knowledge.jsonl", payload)
    private_write(
        output / "targeted-medical-report.json",
        json.dumps(summary, ensure_ascii=False, indent=2).encode(),
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noncommercial-use", action="store_true")
    args = parser.parse_args()
    try:
        summary = collect(
            Path("data/processed"),
            Path("data/raw/targeted-medical"),
            noncommercial=args.noncommercial_use,
        )
    except (CollectionError, OSError, ValueError) as error:
        parser.exit(1, f"Targeted medical collection failed: {error}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
