import json
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from silvercare.data import DatasetManifest, chunks, ingest, prepare, raw_records
from silvercare.privacy import redact
from tests.test_azure import make_services


def manifest(**overrides):
    values = {
        "dataset_id": "test-dataset",
        "title": "어르신 대화",
        "source_url": "https://aihub.or.kr/dataset",
        "evidence_kind": "dialogue",
        "license": "test-only",
        "license_reviewed": True,
        "human_reviewed": False,
        "updated_at": "2026-11-09",
        "text_paths": ["utterances.*.text"],
    }
    return DatasetManifest(**(values | overrides))


def test_redaction_does_not_remove_family_terms():
    text = "부모님과 어머니를 돌봅니다. 1577-1000, 1899-9988, 119"
    assert redact(text) == text
    private = "이름: 홍길동\n010-1234-5678 a@example.com 900101-1234567\n주소: 서울시 101동"
    cleaned = redact(private)
    for token in ("홍길동", "010-1234", "a@example", "900101", "서울시"):
        assert token not in cleaned
    assert redact(cleaned) == cleaned


def test_chunk_threshold_overlap_and_boundaries():
    text = "가" * 2500
    pieces = list(chunks(text))
    assert [len(item) for item in pieces] == [1000, 1000, 740]
    with pytest.raises(ValueError):
        list(chunks(text, size=100, overlap=100))


def test_chunk_splitting_always_advances_with_large_valid_overlap():
    code = (
        "import json;from silvercare.data import chunks;"
        "print(json.dumps(list(chunks('a'*25+'\\n'+'b'*70,size=40,overlap=35))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=5
    )
    pieces = json.loads(result.stdout)
    assert pieces and len(pieces) <= 20
    assert all(20 <= len(piece) <= 40 for piece in pieces)
    assert pieces[0].startswith("a" * 25)
    assert pieces[-1].endswith("b" * 20)


def test_prepare_extracts_only_explicit_text_and_deduplicates(tmp_path):
    source = tmp_path / "input.json"
    record = {
        "name": "비밀이름",
        "utterances": [
            {"text": "어르신의 하루를 함께 돌봅니다. 전화번호는 010-1234-5678입니다."},
            {"text": "편안한 마음으로 이야기해 주세요."},
        ],
    }
    source.write_text(json.dumps([record, record], ensure_ascii=False))
    output = tmp_path / "processed.jsonl"
    counts = prepare(manifest(), source, output)
    assert counts == {"records": 2, "redacted_records": 2, "duplicates": 1, "chunks": 1}
    text = output.read_text()
    assert "비밀이름" not in text
    assert "010-1234" not in text
    assert json.loads(text)["evidence_kind"] == "dialogue"
    assert output.stat().st_mode & 0o777 == 0o600


def test_empty_schema_does_not_replace_previous_output(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text('{"unknown":"text"}')
    output = tmp_path / "out.jsonl"
    output.write_text("prior output")
    with pytest.raises(ValueError, match="No usable"):
        prepare(manifest(), source, output)
    assert output.read_text() == "prior output"
    assert not output.with_suffix(".jsonl.partial").exists()


def test_license_and_evidence_gates(tmp_path):
    with pytest.raises(ValueError):
        manifest(evidence_kind="reference")
    with pytest.raises(ValueError, match="usage rights"):
        prepare(manifest(license_reviewed=False), None, tmp_path / "out.jsonl")
    with pytest.raises(ValueError):
        manifest(source_url="javascript:alert(1)")


def test_archive_is_read_without_extracting_paths(tmp_path):
    archive_path = tmp_path / "test.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../../escaped.txt", "어르신과 함께 나누는 편안한 일상 대화입니다.")
    assert len(list(raw_records(archive_path, []))) == 1
    assert not (tmp_path.parent / "escaped.txt").exists()


async def test_human_review_required_before_upload(tmp_path):
    service = make_services(lambda request: pytest.fail("Must fail before network"))
    try:
        with pytest.raises(ValueError, match="privacy review"):
            await ingest(manifest(), Path("unused"), service)
    finally:
        await service.close()


async def test_search_partial_batch_failure_is_not_success(tmp_path):
    reviewed = manifest(human_reviewed=True)
    source = tmp_path / "input.txt"
    source.write_text("어르신과 함께 나누는 편안한 일상 대화입니다.")
    output = tmp_path / "out.jsonl"
    prepare(reviewed, source, output)

    def handler(request):
        if request.method == "PUT":
            schema = json.loads(request.content)
            assert schema["fields"][1]["analyzer"] == "ko.microsoft"
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"value": [{"status": False}]})

    service = make_services(handler)
    try:
        with pytest.raises(ValueError, match="incomplete"):
            await ingest(reviewed, output, service)
    finally:
        await service.close()
