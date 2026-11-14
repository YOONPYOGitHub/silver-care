import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from silvercare import clinical_sources as clinical
from silvercare.external_sources import CollectionError, ProviderReport, SourceHTML


def table(html):
    return next(node for node in SourceHTML(html.encode()).root.elements() if node.tag == "table")


def test_table_export_preserves_original_cell_coordinates_and_merged_headers():
    result = clinical.matrix(
        table(
            "<table><tr><th rowspan='2'>Identity</th><th colspan='2'>Original group</th></tr>"
            "<tr><th>First</th><th>Second</th></tr>"
            "<tr><td>synthetic</td><td>value A</td><td>value B</td></tr></table>"
        )
    )
    assert result["rows"] == result["columns"] == 3
    assert result["cells"][0]["rowspan"] == 2
    assert result["cells"][1]["colspan"] == 2
    assert result["cells"][-1]["column"] == 2
    assert result["cells"][-1]["text"] == "value B"


@pytest.mark.parametrize(
    "html",
    [
        "<table></table>",
        "<table><tr><td rowspan='2'>bad</td></tr></table>",
        "<table><tr><td colspan='31'>bad</td></tr></table>",
        "<table><tr><td colspan='x'>bad</td></tr></table>",
        "<table><tr><th>A</th><th>B</th></tr><tr><td>missing</td></tr></table>",
    ],
)
def test_invalid_or_incomplete_table_grids_fail_instead_of_guessing(html):
    with pytest.raises(CollectionError) as error:
        clinical.matrix(table(html))
    assert error.value.code == "table_shape"


def fda_html():
    headers = "<th title='Drug or Other Substance'>Drug</th>" + "".join(
        f"<th>Original role {index}</th>" for index in range(10)
    )
    rows = "".join(
        f"<tr><td>synthetic-{index}</td>" + "<td></td>" * 10 + "</tr>" for index in range(100)
    )
    definitions = "".join(
        f"<table class='table-bordered' summary='{name}'>"
        "<tr><th>Original heading</th><th>Original definition</th></tr>"
        "<tr><td>Fixture category</td><td>Fixture definition</td></tr></table>"
        for name in ("Inhibitor", "Inducer", "Substrate", "Inhibitor", "Substrate")
    )
    return (
        "<h1>For Healthcare Professionals | FDA’s Examples of Drugs that Interact with CYP</h1>"
        "<div role='main'></div><p>Original article note.</p>"
        f"<table class='table-bordered' summary='Table 1.'><tr>{headers}</tr>{rows}</table>"
        + definitions
        + "<time datetime='2026-05-29T14:00:00Z'>05/29/2026</time>"
        "<footer><p>EXCLUDED FOOTER</p><table><tr><td>EXCLUDED TABLE</td></tr></table></footer>"
    ).encode()


def test_fda_scopes_exact_scientific_tables_even_when_the_article_markup_closes_early():
    records, structured = clinical.fda_cyp_records(fda_html())
    assert len(records) == 105
    assert len(structured["tables"]) == 6
    assert structured["tables"][0]["columns"] == 11
    assert structured["notes"] == ["Original article note."]
    assert "Blank cells do not establish" in records[0].text
    assert all(record.audience == "provider" for record in records)


def test_fda_missing_column_or_changed_table_identity_is_a_failure():
    for data in [
        fda_html().replace(b"Table 1.", b"Other table"),
        fda_html().replace(b"<th>Original role 0</th>", b""),
    ]:
        with pytest.raises(CollectionError) as error:
            clinical.fda_cyp_records(data)
        assert error.value.code == "table_shape"


def test_whole_collection_requires_explicit_noncommercial_attestation_before_http(tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: pytest.fail("No network before WHO license attestation")
        )
    ) as client:
        with pytest.raises(CollectionError) as error:
            clinical.collect(tmp_path, tmp_path / "raw", client=client)
    assert error.value.code == "noncommercial_attestation_required"
    assert not list(tmp_path.iterdir())


def test_collection_failure_does_not_replace_previously_prepared_data(tmp_path):
    output = tmp_path / "clinical-knowledge.jsonl"
    output.write_text("previous validated corpus\n")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="A different publication without a license")
        )
    ) as client:
        with pytest.raises(CollectionError):
            clinical.collect(tmp_path, tmp_path / "raw", noncommercial=True, client=client)
    assert output.read_text() == "previous validated corpus\n"
    report = json.loads((tmp_path / "clinical-collection-report.json").read_text())
    assert report["status"] == "failed"
    assert report["previous_corpus_retained"]
    assert not (tmp_path / "clinical-structured.json").exists()


def test_changed_pdf_is_rejected_before_parsing(monkeypatch):
    monkeypatch.setattr(
        clinical.pdfplumber, "open", lambda _: pytest.fail("Do not parse new edition")
    )
    with pytest.raises(CollectionError) as error:
        clinical.pdf_records(
            b"changed",
            expected_hash=clinical.WHO_SHA256,
            expected_pages=168,
            selections=clinical.WHO_PAGES,
            who=True,
            report=ProviderReport("who"),
        )
    assert error.value.code == "edition_changed"


@pytest.mark.parametrize("valid_license", [False, True])
def test_checked_pdf_regions_have_license_and_record_explicit_layout_omissions(
    monkeypatch, valid_license
):
    class Page:
        width, height, images = 841.89, 595.276, []

        def find_tables(self):
            return []

        def extract_text(self):
            return (
                "978-92-4-010372-6 CC BY-NC-SA 3.0 IGO non-commercial"
                if valid_license
                else "No applicable reuse license"
            )

        def extract_words(self):
            return [{"text": "crossing", "x0": 190, "x1": 220, "top": 30}]

        def filter(self, predicate):
            assert predicate({"object_type": "char", "x0": 510, "x1": 520, "top": 50, "bottom": 60})
            return self

        def crop(self, bounds):
            class Region:
                def extract_text(self):
                    return "Synthetic complete narrative for layout validation. " * 12

            return Region()

    class PDF:
        pages = [Page() for _ in range(168)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(clinical.pdfplumber, "open", lambda _: PDF())
    data = b"synthetic"
    report = ProviderReport("who")
    kwargs = {
        "expected_hash": hashlib.sha256(data).hexdigest(),
        "expected_pages": 168,
        "selections": {"Clinical scope": tuple(range(13, 33))},
        "who": True,
        "report": report,
    }
    if not valid_license:
        with pytest.raises(CollectionError) as error:
            clinical.pdf_records(data, **kwargs)
        assert error.value.code == "reuse_permission_required"
        return
    records = clinical.pdf_records(data, **kwargs)
    assert len(records) == 20
    assert all(record.audience == "provider" for record in records)
    assert all("#page=" in record.url for record in records)
    assert "crossing layout" in report.notes[-1]
    assert all(record.year == "2024" and record.updated == "2025-09-22" for record in records)


def label():
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "set_id": "22222222-2222-2222-2222-222222222222",
        "effective_time": "20261001",
        "version": "2",
        "openfda": {
            "generic_name": ["GABAPENTIN"],
            "substance_name": ["GABAPENTIN"],
            "brand_name": ["Synthetic fixture"],
            "route": ["ORAL"],
            "application_number": ["ANDA123456"],
        },
        "drug_interactions": ["Synthetic original interaction statement, not dosing advice."],
        "dosage_and_administration": ["Synthetic renal-function condition with a source table."],
        "dosage_and_administration_table": [
            "<table><tr><th>Original condition</th><th>Original value</th></tr>"
            "<tr><td>fixture condition</td><td>fixture value</td></tr></table>"
        ],
        "geriatric_use": ["Synthetic geriatric-use statement."],
    }


def applications():
    return [
        {
            "application_number": "ANDA123456",
            "submissions": [{"submission_status": "AP"}],
            "products": [
                {
                    "marketing_status": "Prescription",
                    "route": "ORAL",
                    "active_ingredients": [{"name": "GABAPENTIN"}],
                }
            ],
        }
    ]


def test_label_exports_full_original_sections_tables_and_application_provenance():
    source = label()
    records, structured = clinical.label_records(source, "GABAPENTIN", applications())
    assert (
        structured["sections"]["dosage_and_administration"] == source["dosage_and_administration"]
    )
    assert (
        structured["table_html"]["dosage_and_administration"]
        == source["dosage_and_administration_table"]
    )
    assert (
        structured["tables"]["dosage_and_administration"][0]["cells"][-1]["text"] == "fixture value"
    )
    assert structured["table_sections_not_flattened_into_online_text"] == [
        "dosage_and_administration"
    ]
    assert all(record.audience == "provider" for record in records)
    assert all(record.updated == "2026-10-01" for record in records)
    dosage = next(
        record for record in records if record.record_id.endswith("dosage_and_administration")
    )
    assert "fixture value" not in dosage.text
    assert "not flattened" in dosage.text
    assert "id%3A" in dosage.url


@pytest.mark.parametrize(
    "change",
    [
        {"effective_time": "20260231"},
        {"drug_interactions": []},
        {"drug_interactions": "wrong shape"},
        {"drug_interactions": ["Copyright third-party statement"]},
        {"openfda": {"generic_name": ["OTHER"], "route": ["ORAL"]}},
    ],
)
def test_label_identity_shape_and_rights_changes_fail(change):
    source = label()
    source.update(change)
    with pytest.raises(CollectionError):
        clinical.label_records(source, "GABAPENTIN", applications())


def test_wrong_or_incomplete_approval_identity_is_not_silently_trusted():
    for mutation in ("number", "route", "ingredient", "approval"):
        approval = deepcopy(applications())
        if mutation == "number":
            approval[0]["application_number"] = "NDA999999"
        elif mutation == "route":
            approval[0]["products"][0]["route"] = "TOPICAL"
        elif mutation == "ingredient":
            approval[0]["products"][0]["active_ingredients"][0]["name"] = "OTHER"
        else:
            approval[0]["submissions"] = []
        with pytest.raises(CollectionError) as error:
            clinical.label_records(label(), "GABAPENTIN", approval)
        assert error.value.code == "label_identity"


@pytest.mark.parametrize(
    "data,status", [(b"not json", 200), (b'{"error":{"code":"NOT_FOUND"}}', 404), (b"[]", 200)]
)
def test_api_failure_is_not_an_empty_successful_dataset(data, status):
    with pytest.raises(CollectionError):
        clinical.json_response(data, status)
