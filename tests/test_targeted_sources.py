import pytest

from silvercare.external_sources import CollectionError, Record
from silvercare.targeted_sources import (
    BLS_LICENSE,
    airway_section,
    collect,
    scoped_records,
    selected_sections,
)


def article(body):
    return ("<main><h1>Test medical article</h1>" + body + "</main>").encode()


def test_selected_sections_keep_conditions_and_exclude_unselected_content():
    first = "Evidence differs by formulation and patient population. " * 3
    second = "Benefits remain uncertain; this is not a personal prescription. " * 3
    data = article(
        f"<h2>Selected</h2><p>{first}</p><h4>More</h4><p>{second}</p>"
        "<h2>Excluded</h2><p>Unselected material must not be indexed.</p>"
    )
    sections = selected_sections(data, "Test medical article", ("Selected",))
    assert first.strip() in sections["Selected"]
    assert second.strip() in sections["Selected"]
    assert "Unselected" not in sections["Selected"]


@pytest.mark.parametrize(
    "data",
    [
        b"<main><h1>Wrong title</h1></main>",
        article("<h2>Different heading</h2><p>Missing selected evidence</p>"),
        article("<h2>Selected</h2><p>Too short</p>"),
        article("<h2>Selected</h2><h2>Selected</h2>"),
    ],
)
def test_source_identity_or_missing_sections_fail_closed(data):
    with pytest.raises(CollectionError):
        selected_sections(data, "Test medical article", ("Selected",))


def test_health_topic_summary_can_be_scoped_without_ingesting_linked_encyclopedia():
    text = "Symptoms can be delayed; check the condition-specific warning signs. " * 3
    data = (
        "<h1>Test medical article</h1><article><h2>Summary</h2>"
        f"<p>{text}</p><h2>Start Here</h2>"
        "<a href='/ency/copyrighted.htm'>Excluded linked encyclopedia</a></article>"
    ).encode()
    result = selected_sections(data, "Test medical article", ("Summary",))
    assert text.strip() in result["Summary"]
    assert "encyclopedia" not in result["Summary"]


def test_adult_airway_extraction_keeps_order_and_stopping_conditions_only():
    data = (
        f'<a href="{BLS_LICENSE}">License</a>'
        "<h1>2025 Korean Guidelines</h1><div>"
        "<h4 id='sec07'>FOREIGN BODY AIRWAY OBSTRUCTION</h4>"
        "<p>If unable to cough effectively, use five back blows, then abdominal thrusts. "
        "Stop if unresponsive and obtain emergency help.</p>"
        "<h4 id='sec08'>DROWNING</h4><p>Excluded other emergency protocol.</p></div>"
    ).encode()
    text = airway_section(data)
    assert "unable to cough effectively" in text
    assert "unresponsive" in text
    assert "DROWNING" not in text
    with pytest.raises(CollectionError, match="license"):
        airway_section(data.replace(BLS_LICENSE.encode(), b"https://example.test/no-license"))


def test_scoped_chunks_repeat_audience_and_source_conditions():
    records = [
        Record(
            "example:section",
            "Synthetic evidence",
            "Condition-specific evidence. " * 140,
            "https://example.test/source",
            audience="caregiver",
        )
    ]
    prepared = scoped_records(records, "Adult rescuer only; preserve consciousness conditions.\n")
    assert len(prepared) > 1
    assert all(record.audience == "caregiver" for record in prepared)
    assert all(record.text.startswith("Adult rescuer only;") for record in prepared)
    assert all(len(record.text) <= 1600 for record in prepared)


def test_noncommercial_attestation_required_before_collection(tmp_path):
    with pytest.raises(CollectionError, match="noncommercial"):
        collect(tmp_path, tmp_path / "raw", noncommercial=False)
    assert not list(tmp_path.iterdir())
