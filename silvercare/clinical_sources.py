"""Collect bounded, licensed clinical references; never make prescribing decisions."""

import argparse
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pdfplumber

from silvercare.config import DataSourceSettings
from silvercare.data import SearchDocument, chunks
from silvercare.external_sources import (
    FDA_POLICY,
    CollectionError,
    Collector,
    ProviderReport,
    Record,
    SourceElement,
    SourceHTML,
    document_records,
    html_text,
    private_write,
    source_date,
)
from silvercare.models import WHO_REUSE_NOTICE
from silvercare.privacy import normalize

WHO_PAGE = "https://www.who.int/publications/i/item/9789240103726"
WHO_PDF = (
    "https://iris.who.int/server/api/core/bitstreams/7516b015-e205-43d1-883f-01f2c31af261/content"
)
WHO_SHA256 = "7addb319b953fe56306e093148431f606bb58feaed3b6557293f93cfa108f5eb"
WHO_LICENSE = (
    "WHO original: CC BY-NC-SA 3.0 IGO, https://creativecommons.org/licenses/by-nc-sa/3.0/igo/; "
    "extracted/normalized text adaptation: CC BY-NC-SA 4.0, "
    "https://creativecommons.org/licenses/by-nc-sa/4.0/; noncommercial use only"
)
WHO_LICENSE += "; " + WHO_REUSE_NOTICE
VA_PAGE = "https://www.healthquality.va.gov/HEALTHQUALITY/guidelines/WRC/PIP/index.asp"
VA_PDF = (
    "https://www.healthquality.va.gov/HEALTHQUALITY/guidelines/WRC/PIP/"
    "PIP-CPG_2026-Guideline_Final_20261020.pdf"
)
VA_POLICY = "https://department.va.gov/copyright-policy/"
VA_SHA256 = "d1a927f540c70dcb0fcc43f0832edb370b2acc309947aaa85b42bf225c5534b7"
FDA_CYP = (
    "https://www.fda.gov/drugs/drug-interactions-labeling/"
    "healthcare-professionals-fdas-examples-drugs-interact-cyp-enzymes-and-transporter-systems"
)
OPENFDA_LABEL = "https://api.fda.gov/drug/label.json"
OPENFDA_APPROVAL = "https://api.fda.gov/drug/drugsfda.json"
OPENFDA_TERMS = "https://open.fda.gov/terms/"
OPENFDA_FIELDS = (
    "boxed_warning",
    "contraindications",
    "drug_interactions",
    "dosage_and_administration",
    "use_in_specific_populations",
    "geriatric_use",
)
LABEL_INGREDIENTS = {
    "APIXABAN": "아픽사반",
    "RIVAROXABAN": "리바록사반",
    "DABIGATRAN ETEXILATE": "다비가트란 에텍실레이트",
    "SITAGLIPTIN": "시타글립틴",
    "GABAPENTIN": "가바펜틴",
    "PREGABALIN": "프레가발린",
    "MEMANTINE HYDROCHLORIDE": "메만틴 염산염",
    "ROSUVASTATIN CALCIUM": "로수바스타틴 칼슘",
    "DIGOXIN": "디곡신",
}
LABEL_FIELD_TITLES = {
    "boxed_warning": "중요 경고",
    "contraindications": "금기",
    "drug_interactions": "약물 상호작용",
    "dosage_and_administration": "용법용량·신기능 관련 조건",
    "use_in_specific_populations": "신장애·특수집단 관련 조건",
    "geriatric_use": "고령자 관련 조건",
}
WHO_PAGES = {
    "통합 돌봄·복합적인 건강 문제와 개인별 계획": (
        30,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        54,
        55,
        56,
        57,
        58,
        59,
        60,
        61,
        62,
        63,
        64,
    ),
    "이동 기능 저하·근력·노쇠 관련 관리": (78, 79, 80, 81, 82, 83, 84),
    "영양불량·근감소·삼킴과 복합질환": (88, 89, 90, 91, 92, 93, 94, 95, 96),
}
VA_PAGES = {
    "욕창 위험 평가·예방": tuple(range(36, 57)),
    "욕창 상처 관리·근거의 강도와 한계": tuple(range(60, 74)),
    "욕창 임상 평가·기록·영양 평가": (128, 129, 130, 131),
}


def require_text(data: bytes, phrases: tuple[str, ...], code: str) -> None:
    text = html_text(data.decode("utf-8"))
    if not all(phrase in text for phrase in phrases):
        raise CollectionError(code, "Source identity or reuse policy changed; review required")


def matrix(table: SourceElement) -> dict[str, Any]:
    """Preserve cell positions and spans; never infer clinical column meanings."""
    rows = [node for node in table.elements() if node.tag == "tr"]
    if not rows or len(rows) > 400:
        raise CollectionError("table_shape", "Table row budget exceeded or empty")
    cells: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    width = 0
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row.children:
            if not isinstance(cell, SourceElement) or cell.tag not in {"td", "th"}:
                continue
            while (row_index, column) in occupied:
                column += 1
            try:
                rowspan = int(cell.attrs.get("rowspan", "1"))
                colspan = int(cell.attrs.get("colspan", "1"))
            except ValueError:
                raise CollectionError("table_shape", "Table span is not an integer") from None
            if not 1 <= rowspan <= len(rows) - row_index or not 1 <= colspan <= 30:
                raise CollectionError("table_shape", "Invalid table cell span")
            positions = {
                (r, c)
                for r in range(row_index, row_index + rowspan)
                for c in range(column, column + colspan)
            }
            if occupied & positions or column + colspan > 30:
                raise CollectionError("table_shape", "Overlapping or oversized table cells")
            occupied.update(positions)
            cells.append(
                {
                    "row": row_index,
                    "column": column,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "header": cell.tag == "th",
                    "title": cell.attrs.get("title", ""),
                    "text": cell.text(),
                }
            )
            column += colspan
            width = max(width, column)
    if not width or any((r, c) not in occupied for r in range(len(rows)) for c in range(width)):
        raise CollectionError("table_shape", "Table grid has missing cells")
    return {"rows": len(rows), "columns": width, "cells": cells}


def pdf_records(
    data: bytes,
    *,
    expected_hash: str,
    expected_pages: int,
    selections: dict[str, tuple[int, ...]],
    who: bool,
    report: ProviderReport,
) -> list[Record]:
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise CollectionError(
            "edition_changed", "PDF changed; review page regions before ingestion"
        )
    records: list[Record] = []
    omitted: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if len(pdf.pages) != expected_pages:
            raise CollectionError("edition_changed", "Unexpected PDF page count")
        if who:
            license_page = normalize(pdf.pages[3].extract_text() or "")
            if not all(
                phrase in license_page
                for phrase in ("978-92-4-010372-6", "CC BY-NC-SA 3.0 IGO", "non-commercial")
            ):
                raise CollectionError("reuse_permission_required", "WHO edition license missing")
        for chapter, page_numbers in selections.items():
            for number in page_numbers:
                page = pdf.pages[number - 1]
                if (round(page.width), round(page.height)) != ((842, 595) if who else (612, 792)):
                    raise CollectionError("pdf_layout", "PDF page geometry changed")
                exclusions = [table.bbox for table in page.find_tables()] + [
                    (image["x0"], image["top"], image["x1"], image["bottom"])
                    for image in page.images
                ]

                def keep(
                    obj: dict[str, Any],
                    boxes: tuple[tuple[float, float, float, float], ...] = tuple(exclusions),
                ) -> bool:
                    if obj.get("object_type") != "char":
                        return True
                    x = (obj["x0"] + obj["x1"]) / 2
                    y = (obj["top"] + obj["bottom"]) / 2
                    return not any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in boxes)

                filtered = page.filter(keep)
                regions = (
                    ((195, 10, 500, 575), (505, 10, 825, 575)) if who else ((40, 60, 575, 740),)
                )
                words = filtered.extract_words()
                for index, bounds in enumerate(regions):
                    left, top, right, bottom = bounds
                    if any(
                        top < word["top"] < bottom
                        and (
                            word["x0"] < left < word["x1"] - 1
                            or word["x0"] + 1 < right < word["x1"]
                        )
                        for word in words
                    ):
                        omitted.append(f"page {number}, column {index + 1}: crossing layout")
                        continue
                    text = normalize(filtered.crop(bounds).extract_text() or "")
                    if len(text) < 150:
                        omitted.append(
                            f"page {number}, column {index + 1}: no substantial narrative"
                        )
                        continue
                    name = "WHO ICOPE" if who else "VA/DoD 욕창 지침"
                    records.append(
                        Record(
                            f"page-{number}-column-{index + 1}",
                            f"{name} · {chapter} · p.{number}",
                            text,
                            f"{WHO_PDF if who else VA_PDF}#page={number}",
                            "2024" if who else "2026",
                            "2025-09-22" if who else "2026-10-20",
                            "provider",
                        )
                    )
    if len(records) < 20:
        raise CollectionError("pdf_layout", "Too few verified narrative regions")
    report.notes.extend(
        [
            f"Exact PDF SHA256 {expected_hash}; {expected_pages} PDF pages. "
            "Edition-specific narrative regions only; images and detected tables excluded. "
            "Not a complete transcription of flowcharts, tools or third-party material.",
            f"Explicitly omitted regions: {json.dumps(omitted)}",
        ]
    )
    return records


def fda_cyp_records(data: bytes) -> tuple[list[Record], dict[str, Any]]:
    root = SourceHTML(data).root
    elements = list(root.elements())
    titles = [node.text() for node in elements if node.tag == "h1"]
    dates = [
        source_date(node.attrs.get("datetime", "")[:10]) for node in elements if node.tag == "time"
    ]
    scopes = [node for node in elements if node.attrs.get("role") == "main"]
    if (
        len(titles) != 1
        or "FDA’s Examples of Drugs that Interact with CYP" not in titles[0]
        or len(dates) != 1
        or not dates[0]
        or len(scopes) != 1
    ):
        raise CollectionError("article_identity", "FDA interaction article identity changed")
    start = next(index for index, node in enumerate(elements) if node.tag == "h1")
    stop = next(index for index, node in enumerate(elements) if node.tag == "time")
    body = elements[start + 1 : stop]
    table_nodes = [
        node for node in body if node.tag == "table" and node.has_class("table-bordered")
    ]
    if [node.attrs.get("summary") for node in table_nodes] != [
        "Table 1.",
        "Inhibitor",
        "Inducer",
        "Substrate",
        "Inhibitor",
        "Substrate",
    ]:
        raise CollectionError("table_shape", "FDA clinical table identities changed")
    tables = [matrix(node) for node in table_nodes]
    if len(tables) != 6 or tables[0]["columns"] != 11 or not 100 <= tables[0]["rows"] <= 400:
        raise CollectionError("table_shape", "FDA clinical interaction table structure changed")
    first = tables[0]
    headers = [cell["title"] or cell["text"] for cell in first["cells"] if cell["row"] == 0]
    if headers[0] != "Drug or Other Substance":
        raise CollectionError("table_shape", "FDA drug identity column missing")
    notes = [node.text() for node in body if node.tag == "p" and node.text()]
    records = []
    for row in range(1, first["rows"]):
        cells = [cell for cell in first["cells"] if cell["row"] == row]
        if len(cells) != 11 or any(cell["rowspan"] != 1 or cell["colspan"] != 1 for cell in cells):
            raise CollectionError("table_shape", "FDA drug row has merged or missing cells")
        values = {header: cell["text"] for header, cell in zip(headers, cells, strict=True)}
        drug = cells[0]["text"]
        records.append(
            Record(
                f"drug-{row}",
                f"FDA 약물 상호작용 기전 · {drug}",
                "Clinical examples; not a complete pairwise interaction/contraindication database. "
                "Blank cells do not establish absence of interaction or safety. "
                "Enzyme/transporter membership alone must not be used to infer a clinical action.\n"
                + json.dumps(values, ensure_ascii=False),
                FDA_CYP,
                dates[0][:4],
                dates[0],
                "provider",
            )
        )
    for index, table in enumerate(tables[1:], 1):
        records.append(
            Record(
                f"definition-{index}",
                f"FDA CYP·수송체 상호작용 정의 · table {index + 1}",
                json.dumps(table, ensure_ascii=False),
                FDA_CYP,
                dates[0][:4],
                dates[0],
                "provider",
            )
        )
    return records, {"title": titles[0], "updated": dates[0], "tables": tables, "notes": notes}


def json_response(data: bytes, status: int) -> dict[str, Any]:
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise CollectionError("invalid_json", "Provider response is not JSON") from None
    if not isinstance(body, dict) or status != 200 or "error" in body:
        raise CollectionError(
            f"api_http_{status}", "Public API returned an error; no corpus refresh"
        )
    return body


def label_records(
    label: dict[str, Any], ingredient: str, applications: list[dict[str, Any]]
) -> tuple[list[Record], dict[str, Any]]:
    metadata = label.get("openfda", {})
    if (
        not isinstance(metadata, dict)
        or ingredient not in metadata.get("generic_name", [])
        or metadata.get("route") != ["ORAL"]
        or not re.fullmatch(r"[a-f0-9-]{36}", label.get("id", ""))
        or not re.fullmatch(r"[a-f0-9-]{36}", label.get("set_id", ""))
    ):
        raise CollectionError(
            "label_identity", "Label ingredient, route or identifiers do not match"
        )
    try:
        updated = datetime.strptime(label.get("effective_time", ""), "%Y%m%d").date().isoformat()
    except ValueError:
        raise CollectionError("label_identity", "Label effective date is invalid") from None
    numbers = metadata.get("application_number", [])
    if not numbers or set(numbers) != {item.get("application_number") for item in applications}:
        raise CollectionError("label_identity", "Drugs@FDA application cross-check is incomplete")
    for application in applications:
        if not any(
            row.get("submission_status") == "AP" for row in application.get("submissions", [])
        ):
            raise CollectionError("label_identity", "No approved application submission found")
        substances = {
            normalize(value).upper() for value in [ingredient, *metadata.get("substance_name", [])]
        }
        if not any(
            product.get("marketing_status") == "Prescription"
            and product.get("route") == "ORAL"
            and any(
                normalize(active.get("name", "")).upper() in substances
                for active in product.get("active_ingredients", [])
            )
            for product in application.get("products", [])
        ):
            raise CollectionError(
                "label_identity", "No matching prescription oral ingredient in application"
            )
    sections: dict[str, list[str]] = {}
    tables: dict[str, list[dict[str, Any]]] = {}
    for name in OPENFDA_FIELDS:
        values = label.get(name, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise CollectionError("label_shape", "Label section has unexpected values")
        if values:
            sections[name] = values
        table_values = label.get(name + "_table", [])
        if not isinstance(table_values, list) or any(
            not isinstance(value, str) for value in table_values
        ):
            raise CollectionError("label_shape", "Label table has unexpected values")
        if table_values:
            tables[name] = [
                matrix(node)
                for value in table_values
                for node in SourceHTML(value.encode()).root.elements()
                if node.tag == "table"
            ]
            if not tables[name]:
                raise CollectionError("label_shape", "Table field contains no structured table")
    if not all(name in sections for name in ("drug_interactions", "dosage_and_administration")):
        raise CollectionError("label_shape", "Required interaction or dosage section missing")
    source_text = " ".join(value for values in sections.values() for value in values)
    if not re.search(r"renal|creatinine|eGFR|CrCl", source_text, re.I):
        raise CollectionError("label_shape", "No renal-function source content found")
    if re.search(r"copyright|all rights reserved", source_text, re.I):
        raise CollectionError(
            "reuse_permission_required", "Selected label has separate rights notice"
        )
    query = urlencode({"search": f'id:"{label["id"]}"', "limit": 1})
    url = f"{OPENFDA_LABEL}?{query}"
    brand = ", ".join(metadata.get("brand_name", []))
    records: list[Record] = []
    omitted_tables = []
    for name, values in sections.items():
        if name in tables:
            omitted_tables.append(name)
            text = (
                f"Original label section: {name}. This section contains a table. "
                "Its full verbatim text, table HTML and cell coordinates/spans are preserved "
                "in the structured offline dataset. Numerical table content is not flattened "
                "into online instructions. Consult the complete original label; "
                "do not infer individual doses from this availability record."
            )
        else:
            text = "\n".join(values)
        records.append(
            Record(
                f"{label['set_id']}:{name}",
                f"미국 의약품 라벨 · {LABEL_INGREDIENTS[ingredient]} · {brand} · "
                f"{LABEL_FIELD_TITLES[name]}",
                f"Generic: {ingredient}; brand: {brand}; route: ORAL; "
                f"application: {', '.join(numbers)}; label set: {label['set_id']}; "
                f"label version: {label.get('version', '')}; effective date: {updated}.\n"
                "Application metadata was cross-checked, but this does not verify that this "
                "submitted SPL text is identical to an FDA-approved label or a Korean label. "
                "Do not transfer conditions between products, dosage forms or indications. "
                "No personal prescribing or renal-dose calculation.\n" + text,
                url,
                updated[:4],
                updated,
                "provider",
            )
        )
    return records, {
        "ingredient": ingredient,
        "id": label["id"],
        "set_id": label["set_id"],
        "version": label.get("version", ""),
        "effective_time": label["effective_time"],
        "openfda": metadata,
        "sections": sections,
        "table_html": {name: label[name + "_table"] for name in tables},
        "tables": tables,
        "applications": applications,
        "table_sections_not_flattened_into_online_text": omitted_tables,
        "source_url": url,
    }


def collect(
    output: Path = Path("data/processed"),
    raw: Path = Path("data/raw/clinical"),
    *,
    noncommercial: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    if not noncommercial:
        raise CollectionError(
            "noncommercial_attestation_required",
            "WHO collection requires explicit noncommercial-use attestation",
        )
    if client is None:
        with httpx.Client(
            timeout=httpx.Timeout(90, connect=30),
            headers={"User-Agent": "silver-care-clinical-reference-collector/1.0"},
        ) as owned:
            return collect(output, raw, noncommercial=noncommercial, client=owned)
    collector = Collector(client, DataSourceSettings(), raw)
    reports: list[ProviderReport] = []
    documents: list[SearchDocument] = []
    structured: dict[str, Any] = {
        "operator_attested_noncommercial": True,
        "clinical_review_status": "not_performed",
        "label_data_is_not_a_prescribing_engine": True,
    }
    definitions = (
        ("who-icope-2025", "World Health Organization", WHO_LICENSE),
        (
            "va-pressure-2026",
            "U.S. Department of Veterans Affairs / Department of Defense",
            f"Government-authored VA text; {VA_POLICY}; third-party materials excluded",
        ),
        (
            "fda-cyp-interactions",
            "U.S. Food and Drug Administration",
            f"FDA public-domain original text; {FDA_POLICY}#linking; no endorsement",
        ),
        (
            "openfda-drug-labels",
            "openFDA / U.S. Food and Drug Administration",
            f"CC0 1.0 unless otherwise noted; {OPENFDA_TERMS}; no endorsement",
        ),
    )
    try:
        for dataset, agency, license_text in definitions:
            report = ProviderReport(dataset)
            reports.append(report)
            if dataset == "who-icope-2025":
                page, _ = collector.fetch(report, WHO_PAGE, "who-icope-publication.html")
                if not all(
                    value in page.decode("utf-8")
                    for value in ("9789240103726", "creativecommons.org/licenses/by-nc-sa/3.0/igo")
                ):
                    raise CollectionError(
                        "reuse_permission_required", "WHO publication license missing"
                    )
                data, _ = collector.fetch(report, WHO_PDF, "who-icope-2025.pdf")
                records = pdf_records(
                    data,
                    expected_hash=WHO_SHA256,
                    expected_pages=168,
                    selections=WHO_PAGES,
                    who=True,
                    report=report,
                )
                structured[dataset] = {
                    "original_license": WHO_LICENSE,
                    "adaptation_notice": WHO_REUSE_NOTICE,
                    "source_url": WHO_PAGE,
                    "pdf_sha256": WHO_SHA256,
                    "selection": WHO_PAGES,
                    "operator_attested_noncommercial": True,
                }
            elif dataset == "va-pressure-2026":
                policy, _ = collector.fetch(report, VA_POLICY, "va-copyright-policy.html")
                require_text(
                    policy,
                    ("government-produced materials", "not copyright protected"),
                    "reuse_permission_required",
                )
                data, _ = collector.fetch(report, VA_PDF, "va-pressure-2026.pdf")
                records = pdf_records(
                    data,
                    expected_hash=VA_SHA256,
                    expected_pages=149,
                    selections=VA_PAGES,
                    who=False,
                    report=report,
                )
                structured[dataset] = {
                    "source_url": VA_PAGE,
                    "pdf_sha256": VA_SHA256,
                    "selection": VA_PAGES,
                    "publication": "August 2026",
                    "evidence_cutoff": "2025-03-31",
                }
            elif dataset == "fda-cyp-interactions":
                policy, _ = collector.fetch(report, FDA_POLICY, "fda-website-policies.html")
                require_text(
                    policy,
                    (
                        "Unless otherwise noted",
                        "They are in the public domain",
                        "without the need to obtain permission from FDA",
                    ),
                    "reuse_permission_required",
                )
                data, _ = collector.fetch(report, FDA_CYP, "fda-cyp-interactions.html")
                records, structured[dataset] = fda_cyp_records(data)
            else:
                terms, _ = collector.fetch(report, OPENFDA_TERMS, "openfda-terms.html")
                require_text(
                    terms,
                    (
                        "CC0 1.0 Universal",
                        "even for commercial purposes",
                        "Some data on openFDA may not be public domain",
                    ),
                    "reuse_permission_required",
                )
                records = []
                labels = []
                for ingredient in LABEL_INGREDIENTS:
                    query = (
                        f'openfda.generic_name.exact:"{ingredient}" AND openfda.route.exact:"ORAL"'
                    )
                    filename = re.sub(r"[^a-z0-9]+", "-", ingredient.lower())
                    data, status = collector.fetch(
                        report,
                        OPENFDA_LABEL,
                        f"label-{filename}.json",
                        {"search": query, "limit": "20", "sort": "effective_time:desc"},
                    )
                    results = json_response(data, status).get("results")
                    if not isinstance(results, list) or not results:
                        raise CollectionError("empty_label", "No matching oral labels")
                    candidates = [
                        row
                        for row in results
                        if isinstance(row, dict)
                        and row.get("openfda", {}).get("application_number")
                        and all(
                            re.fullmatch(r"(?:NDA|ANDA)\d{6}", number)
                            for number in row["openfda"]["application_number"]
                        )
                    ]
                    if not candidates:
                        raise CollectionError("label_identity", "No application-linked label")
                    label = max(
                        candidates, key=lambda row: (row.get("effective_time", ""), row["id"])
                    )
                    approvals = []
                    for number in label["openfda"]["application_number"]:
                        data, status = collector.fetch(
                            report,
                            OPENFDA_APPROVAL,
                            f"approval-{number}.json",
                            {"search": f'application_number:"{number}"', "limit": "1"},
                        )
                        rows = json_response(data, status).get("results")
                        if not isinstance(rows, list) or len(rows) != 1:
                            raise CollectionError("label_identity", "Application record missing")
                        approvals.append(rows[0])
                    prepared, metadata = label_records(label, ingredient, approvals)
                    records.extend(prepared)
                    labels.append(metadata)
                structured[dataset] = labels
                report.notes.append(
                    "Nine explicitly selected oral ingredients, "
                    "not all drugs or a pairwise DDI DB. "
                    "Full selected section text and table HTML/cell grids retained offline. "
                    "Table-bearing sections are not flattened into online numerical instructions. "
                    "No API key or patient data sent. No personal dose calculation."
                )
            # Repeat source scope in each indexed chunk rather than separating it from conditions.
            scoped = []
            for record in records:
                notice = (
                    (
                        "WHO ICOPE 의료인용 원문 발췌·정제 자료, 비상업적 CC BY-NC-SA 이용. "
                        "WHO가 작성·검증한 한국어 안내가 아니며 영어 원문이 우선합니다. "
                        "개인별 진단·처방·자가 처치 지시가 아닙니다."
                    )
                    if dataset == "who-icope-2025"
                    else (
                        "의료인용 원문 자료. 일반 근거 설명용이며 "
                        "개인별 진단·처방·자가 처치 지시가 아닙니다. "
                        "미국 자료는 한국 허가·보험 기준을 뜻하지 않습니다."
                    )
                )
                prefix = f"{record.title}\nSource date: {record.updated}.\n{notice}\n"
                for index, part in enumerate(
                    chunks(record.text, size=1600 - len(prefix), overlap=120)
                ):
                    scoped.append(
                        Record(
                            f"{record.record_id}:chunk-{index}",
                            record.title,
                            prefix + part,
                            record.url,
                            record.year,
                            record.updated,
                            record.audience,
                        )
                    )
            prepared_documents, report.duplicates = document_records(
                scoped, dataset, agency, license_text
            )
            for document in prepared_documents:
                document.risk_level = "clinical"
            documents.extend(prepared_documents)
            report.records = len(records)
            report.chunks = len(prepared_documents)
            report.status = "success"
    except CollectionError as error:
        private_write(
            output / "clinical-collection-report.json",
            json.dumps(
                {
                    "status": "failed",
                    "error": {"code": error.code, "message": error.message},
                    "providers": [vars(item) for item in reports],
                    "previous_corpus_retained": True,
                },
                ensure_ascii=False,
                indent=2,
            ).encode(),
        )
        raise
    files = {
        "clinical-knowledge.jsonl": "".join(
            doc.model_dump_json() + "\n" for doc in documents
        ).encode(),
        "clinical-structured.json": json.dumps(structured, ensure_ascii=False, indent=2).encode(),
        "who-icope-adapted.jsonl": "".join(
            doc.model_dump_json() + "\n" for doc in documents if doc.dataset_id == "who-icope-2025"
        ).encode(),
    }
    for name, data in files.items():
        temporary = output / (name + ".partial")
        private_write(temporary, data)
        temporary.replace(output / name)
    summary = {
        "status": "success",
        "collected_at": datetime.now(UTC).isoformat(),
        "operator_attested_noncommercial": True,
        "clinical_review_status": "not_performed",
        "documents": len(documents),
        "providers": [vars(item) for item in reports],
        "files": {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()
        },
    }
    private_write(
        output / "clinical-collection-report.json",
        json.dumps(summary, ensure_ascii=False, indent=2).encode(),
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noncommercial-use", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw/clinical"))
    args = parser.parse_args()
    try:
        report = collect(args.output_dir, args.raw_dir, noncommercial=args.noncommercial_use)
    except (CollectionError, OSError, ValueError) as error:
        message = error.message if isinstance(error, CollectionError) else type(error).__name__
        parser.exit(1, f"Clinical collection failed: {message}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
