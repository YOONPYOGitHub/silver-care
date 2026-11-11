import io
import json
import tarfile
import zipfile

import httpx
import pytest

from silvercare.archives import source_files
from silvercare.corpus import CorpusCatalog
from silvercare.data import DatasetManifest, download_aihub, ingest, prepare
from tests.test_azure import make_services


def tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def zipped_json(value):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("source.json", json.dumps(value, ensure_ascii=False))
    return buffer.getvalue()


def medical_manifest(**overrides):
    return DatasetManifest(
        **{
            "dataset_id": "test-medical",
            "title": "Test care corpus",
            "source_url": "https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71874",
            "evidence_kind": "dataset_knowledge",
            "source_schema": "medical_source",
            "source_file_key": "123",
            "split": "training",
            "license": "synthetic-test-only",
            "license_reviewed": True,
            "review_method": "automated",
            "updated_at": "2026-11-09",
            **overrides,
        }
    )


def medical_record(**overrides):
    return {
        "c_id": "test-1",
        "domain": 1,
        "source": 4,
        "source_spec": "시험 자료",
        "creation_year": "null",
        "content": (
            "노인의 신체활동과 식사에 관한 일반적인 생활 설명입니다. "
            "이 예시는 실제 의학 정보를 제공하지 않는 테스트용 합성 자료입니다. "
            "본문의 문맥을 보존하고 출처를 구분하는 기능을 검증하기 위해 "
            "충분한 길이로 작성했습니다."
        ),
        **overrides,
    }


def test_nested_zip_parts_are_ordered_and_deduplicated(tmp_path):
    zipped = zipped_json({"content": "synthetic"})
    first, last = zipped[:20], zipped[20:]
    path = tmp_path / "source.tar"
    path.write_bytes(
        tar_bytes(
            [
                ("labels.zip.part1", last),
                ("labels.zip.part0", first),
                ("labels.zip.part0", first),
            ]
        )
    )
    rows = list(source_files(path))
    assert len(rows) == 1
    assert json.loads(rows[0][1]) == {"content": "synthetic"}
    assert not (tmp_path / "source.json").exists()


@pytest.mark.parametrize(
    "entries,reason",
    [
        ([("a.zip.part0", b"first"), ("a.zip.part0", b"conflict")], "Conflicting"),
        ([("a.zip.part1", b"missing")], "Missing"),
    ],
)
def test_invalid_parts_are_explicit_errors(tmp_path, entries, reason):
    path = tmp_path / "bad.tar"
    path.write_bytes(tar_bytes(entries))
    with pytest.raises(ValueError, match=reason):
        list(source_files(path))


def test_archive_links_are_rejected(tmp_path):
    path = tmp_path / "link.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(ValueError, match="links"):
        list(source_files(path))


def test_medical_preparation_classifies_instead_of_banning_clinical_words(tmp_path):
    rows = [
        medical_record(),
        medical_record(c_id="dose", content="노인에게 약물 5mg을 투여합니다. " * 12),
        medical_record(c_id="private", content="노인 보호자 연락처 010-1234-5678입니다. " * 8),
        medical_record(c_id="short", content="노인 식사."),
    ]
    path = tmp_path / "input.zip"
    path.write_bytes(zipped_json(rows))
    output = tmp_path / "out.jsonl"
    counts = prepare(medical_manifest(), path, output)
    assert counts["records"] == 4
    assert counts["chunks"] == 2
    assert counts["sensitive_pattern"] == 1
    assert counts["incomplete_fragment"] == 1
    documents = [json.loads(line) for line in output.read_text().splitlines()]
    document = documents[0]
    assert document["content"] == rows[0]["content"]
    assert document["source_record_id"] == "test-1"
    assert document["source_year"] == ""
    assert document["review_status"] == "automated"
    assert document["evidence_kind"] == "dataset_knowledge"
    assert documents[1]["risk_level"] == "clinical"
    assert "5mg" in documents[1]["content"]
    assert output.with_suffix(".report.json").is_file()
    assert output.stat().st_mode & 0o777 == 0o600


def test_holdout_and_unqualified_knowledge_are_rejected():
    assert medical_manifest(split="validation").split == "validation"
    with pytest.raises(ValueError, match="schema"):
        medical_manifest(source_schema="generic")
    with pytest.raises(ValueError):
        CorpusCatalog.model_validate(
            {
                "sources": [
                    {
                        "name": "bad",
                        "dataset_id": "71874",
                        "file_key": "123",
                        "purpose": "knowledge",
                        "raw_path": "raw.tar",
                    }
                ]
            }
        )


async def test_automated_pilot_requires_explicit_upload_option(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(zipped_json([medical_record()]))
    output = tmp_path / "out.jsonl"
    manifest = medical_manifest()
    prepare(manifest, source, output)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "PUT":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"value": [{"status": True}]})

    services = make_services(handler)
    try:
        with pytest.raises(ValueError, match="privacy review"):
            await ingest(manifest, output, services)
        assert not calls
        assert await ingest(manifest, output, services, allow_automated_review=True) == 1
    finally:
        await services.close()


def test_selected_ids_fail_closed_when_source_record_is_missing(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(zipped_json([medical_record()]))
    output = tmp_path / "out.jsonl"
    output.write_text("previous")
    with pytest.raises(ValueError, match="Selected record IDs"):
        prepare(medical_manifest(selected_record_ids=["missing"]), source, output)
    assert output.read_text() == "previous"


def test_medical_validation_does_not_echo_private_input():
    from silvercare.data import MedicalSource

    with pytest.raises(ValueError) as error:
        MedicalSource.model_validate(medical_record(content={"secret": "private-marker"}))
    assert "private-marker" not in str(error.value)


async def test_download_redirect_does_not_forward_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AIHUB_API_KEY", "synthetic-secret")
    content = tar_bytes([("source.txt", b"test")])
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "api.aihub.or.kr":
            assert request.headers["apikey"] == "synthetic-secret"
            return httpx.Response(
                302,
                headers={
                    "location": "https://stream.aihub.or.kr/shellStream.do?ticket=synthetic-ticket",
                },
            )
        assert request.url.host == "stream.aihub.or.kr"
        assert "apikey" not in request.headers
        return httpx.Response(200, content=content, headers={"content-type": "application/x-tar"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("silvercare.data.httpx.AsyncClient", lambda **kwargs: client)
    output = tmp_path / "data.tar"
    assert await download_aihub("86", "66048", output, 1) == len(content)
    assert len(requests) == 2
    assert tarfile.is_tarfile(output)


@pytest.mark.parametrize(
    "destination",
    [
        "https://evil.example/shellStream.do?ticket=test",
        "http://stream.aihub.or.kr/shellStream.do",
        "https://stream.aihub.or.kr/other-path",
        "https://stream.aihub.or.kr:444/shellStream.do",
    ],
)
async def test_download_rejects_untrusted_redirects(tmp_path, monkeypatch, destination):
    monkeypatch.setenv("AIHUB_API_KEY", "synthetic-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": destination})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("silvercare.data.httpx.AsyncClient", lambda **kwargs: client)
    with pytest.raises(ValueError, match="untrusted"):
        await download_aihub("86", "66048", tmp_path / "data.tar", 1)
    assert len(requests) == 1
    assert not (tmp_path / "data.tar").exists()


async def test_nonarchive_response_preserves_previous_download(tmp_path, monkeypatch):
    monkeypatch.setenv("AIHUB_API_KEY", "synthetic-secret")
    output = tmp_path / "data.tar"
    output.write_bytes(b"previous")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not an archive")
        )
    )
    monkeypatch.setattr("silvercare.data.httpx.AsyncClient", lambda **kwargs: client)
    with pytest.raises(ValueError, match="valid TAR"):
        await download_aihub("86", "66048", output, 1)
    assert output.read_bytes() == b"previous"
    assert not output.with_suffix(".tar.partial").exists()
