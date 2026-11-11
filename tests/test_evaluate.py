import json

import httpx
import pytest

from silvercare import evaluate
from tests.test_azure import make_services


@pytest.mark.parametrize(
    "answer,required_source,passed",
    [
        ("어떤 도움이 필요하신가요?", "", True),
        ("어르신의 뜻을 여쭤보세요.", "", False),
        ("웹채팅으로 상담할 수 있습니다.", "https://example.test/required", False),
    ],
)
async def test_audience_report_detects_regression_and_tracks_exact_inputs(
    tmp_path, monkeypatch, answer, required_source, passed
):
    monkeypatch.chdir(tmp_path)
    corpus = tmp_path / "data/processed/expanded"
    corpus.mkdir(parents=True)
    for name in ("knowledge.jsonl", "cases.jsonl"):
        (corpus / name).write_text("{}\n")
    path = tmp_path / "scenarios.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "self",
                        "request": {"message": "돌봄 서비스를 알고 싶어요."},
                        "forbidden": ["어르신의 뜻을"],
                        "required_source_url": required_source,
                    }
                ]
            }
        )
    )

    def handler(request):
        if "/docs/search" in str(request.url):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": answer}]}],
            },
        )

    service = make_services(handler)
    monkeypatch.setattr(evaluate, "AzureServices", lambda settings: service)
    output = tmp_path / "report.json"
    report = await evaluate.evaluate_audience(path, output)
    assert report["passed"] is passed
    assert report["cases"][0]["passed"] is passed
    assert len(report["prompt_sha256"]) == len(report["scenarios_sha256"]) == 64
    assert len(report["knowledge_sha256"]) == len(report["cases_sha256"]) == 64
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o777 == 0o600
