import hashlib
import io
import json
import wave

import httpx
import pytest

from silvercare import evaluate


@pytest.mark.parametrize(
    "reference,hypothesis,expected",
    [("안녕", "안녕", 0), ("안녕", "안뇽", 1), ("가나다", "가다", 1), ("", "가", 1)],
)
def test_speech_edit_distance(reference, hypothesis, expected):
    assert evaluate.edit_distance(reference, hypothesis) == expected


def test_speech_normalization_does_not_replace_words_or_numbers():
    assert evaluate.speech_tokens("안녕하세요! １２ 번.") == ["안녕하세요", "12", "번"]
    assert evaluate.speech_tokens("우울해요") != evaluate.speech_tokens("괜찮아요")


@pytest.fixture
def speech_manifest(tmp_path):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 3200)
    data = buffer.getvalue()
    (tmp_path / "sample.wav").write_bytes(data)
    manifest = {
        "source_url": "https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=107",
        "samples": [
            {
                "audio": "sample.wav",
                "sha256": hashlib.sha256(data).hexdigest(),
                "speaker_id": "synthetic-speaker",
                "age": 62,
                "seconds": 0.1,
                "reference": "안녕하세요.",
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


async def test_speech_evaluation_measures_real_endpoint_output(
    speech_manifest, tmp_path, monkeypatch
):
    def handler(request):
        assert request.url.path == "/api/speech/transcribe"
        assert b'filename="speech.wav"' in request.content
        assert b"synthetic-speaker" not in request.content
        return httpx.Response(200, json={"text": "안녕하세여"})

    client = httpx.AsyncClient(
        base_url="https://speech.example", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(evaluate.httpx, "AsyncClient", lambda **kwargs: client)
    output = tmp_path / "report.json"
    report = await evaluate.evaluate_speech(speech_manifest, output, "https://speech.example")
    assert report["metrics"]["cer"] == 0.2
    assert report["metrics"]["wer"] == 1
    assert report["metrics"]["samples"] == report["metrics"]["speakers"] == 1
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("filename", ["../sample.wav", "/sample.wav", "folder\\sample.wav"])
async def test_speech_preflight_rejects_nonlocal_paths(speech_manifest, tmp_path, filename):
    data = json.loads(speech_manifest.read_text())
    data["samples"][0]["audio"] = filename
    speech_manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="without directories"):
        await evaluate.evaluate_speech(
            speech_manifest, tmp_path / "report.json", "https://speech.example"
        )


async def test_speech_preflight_rejects_changed_audio(speech_manifest, tmp_path):
    (tmp_path / "sample.wav").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        await evaluate.evaluate_speech(
            speech_manifest, tmp_path / "report.json", "https://speech.example"
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://speech.example",
        "https://user:password@speech.example",
        "https://speech.example/path",
    ],
)
async def test_speech_evaluation_requires_a_plain_https_origin(speech_manifest, tmp_path, origin):
    with pytest.raises(ValueError, match="plain HTTPS"):
        await evaluate.evaluate_speech(speech_manifest, tmp_path / "report.json", origin)
