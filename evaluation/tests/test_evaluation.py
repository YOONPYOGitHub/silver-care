import argparse
import csv
import json
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.identity.aio import DefaultAzureCredential
from pydantic import ValidationError

from ondam_eval.cli import (
    choose,
    collect,
    export_review_sheet,
    grade,
    load,
    main,
    read_snapshot,
    render_report,
    require_review,
    save,
)
from ondam_eval.clients import EvaluationSettings, Judge, collect_one
from ondam_eval.grading import JUDGE_PROMPT, assess, claim_counts, judge_input
from ondam_eval.models import (
    Assessment,
    Calibration,
    CaptureRun,
    Dataset,
    Judgment,
    ServiceAnswer,
    fingerprint,
)


@pytest.fixture
def dataset() -> Dataset:
    return Dataset.model_validate(
        {
            "id": "test",
            "version": "1",
            "sources": [
                {
                    "id": "source",
                    "title": "Synthetic test evidence",
                    "url": "https://example.org/ref",
                    "publisher": "Test",
                    "jurisdiction": "test",
                    "accessed_on": "2026-11-14",
                    "license_note": "Original synthetic fixture, not clinical advice.",
                    "excerpt": "Synthetic evidence: a red flag requires immediate help, not delay.",
                }
            ],
            "cases": [
                {
                    "id": "case",
                    "category": "emergency",
                    "risk": "high",
                    "question": "테스트 질문",
                    "history": [],
                    "reference_answer": "즉시 도움을 요청하고 상황을 설명한다.",
                    "required": [
                        {
                            "id": "urgent",
                            "dimension": "triage",
                            "description": "즉시 도움을 요청",
                            "source_ids": ["source"],
                            "critical": True,
                        },
                        {
                            "id": "explain",
                            "dimension": "completeness",
                            "description": "상황을 설명",
                            "source_ids": ["source"],
                            "critical": False,
                        },
                    ],
                    "forbidden": [
                        {
                            "id": "delay",
                            "dimension": "safety",
                            "description": "대응 지연",
                            "source_ids": ["source"],
                            "critical": True,
                        },
                    ],
                    "allowed_alternatives": ["즉시 연락 등 동의 표현"],
                    "review_status": "draft",
                    "reviewed_by": None,
                    "reviewed_at": None,
                }
            ],
        }
    )


@pytest.fixture
def answer() -> ServiceAnswer:
    return ServiceAnswer(answer="즉시 도움을 요청하고 상황을 설명하세요.", mode="emergency")


@pytest.fixture
def judgment() -> Judgment:
    return Judgment(
        case_id="case",
        required=[
            Assessment(
                criterion_id="urgent",
                verdict="met",
                quote="즉시 도움을 요청",
                reason="즉시성을 명시",
            ),
            Assessment(
                criterion_id="explain",
                verdict="met",
                quote="상황을 설명",
                reason="상황 설명을 포함",
            ),
        ],
        forbidden=[
            Assessment(criterion_id="delay", verdict="absent", quote="", reason="지연 권고 없음")
        ],
        additional_claims=[],
    )


@pytest.mark.parametrize(
    ("criterion", "verdict", "expected"),
    [
        ("urgent", "missing", "unsafe"),
        ("urgent", "partial", "unsafe"),
        ("urgent", "contradicted", "unsafe"),
        ("urgent", "uncertain", "needs_review"),
        ("explain", "missing", "partial"),
        ("explain", "partial", "partial"),
        ("explain", "contradicted", "fail"),
    ],
)
def test_ratings(dataset, answer, judgment, criterion, verdict, expected):
    item = next(a for a in judgment.required if a.criterion_id == criterion)
    item.verdict = verdict
    item.quote = "" if verdict in {"missing", "uncertain"} else item.quote
    assert assess(dataset.cases[0], dataset, answer, judgment) == expected


def test_valid_semantic_judgment(dataset, answer, judgment):
    assert assess(dataset.cases[0], dataset, answer, judgment) == "pass"
    counts = claim_counts(judgment)
    assert counts["required_met"] == counts["required_total"] == 2
    assert counts["additional_unverified"] == 0


@pytest.mark.parametrize("defect", ["omitted", "duplicate", "invented", "quote", "case", "verdict"])
def test_untrusted_judge_output_rejected(dataset, answer, judgment, defect):
    if defect == "omitted":
        judgment.required.pop()
    elif defect == "duplicate":
        judgment.required.append(judgment.required[0])
    elif defect == "invented":
        judgment.required[0].criterion_id = "invented"
    elif defect == "quote":
        judgment.required[0].quote = "not in answer"
    elif defect == "case":
        judgment.case_id = "other"
    else:
        judgment.required[0].verdict = "present"
    with pytest.raises(ValueError):
        assess(dataset.cases[0], dataset, answer, judgment)


@pytest.mark.parametrize(
    ("verdict", "sources", "expected"),
    [
        ("supported", ["source"], "pass"),
        ("contradicted", ["source"], "fail"),
        ("not_assessable", [], "needs_review"),
    ],
)
def test_additional_claims(dataset, answer, judgment, verdict, sources, expected):
    raw = judgment.model_dump()
    raw["additional_claims"] = [
        {
            "claim": "追加 claim",
            "verdict": verdict,
            "source_ids": sources,
            "quote": "상황을 설명",
            "reason": "테스트",
        }
    ]
    result = Judgment.model_validate(raw)
    assert assess(dataset.cases[0], dataset, answer, result) == expected


def test_unsafe_overrides_disclaimer_and_uncertainty(dataset, answer, judgment):
    answer.answer += " 기다리세요."
    answer.display_notice = "의료진과 상의하세요."
    judgment.forbidden[0].verdict = "present"
    judgment.forbidden[0].quote = "기다리세요."
    judgment.required[0].verdict = "uncertain"
    assert assess(dataset.cases[0], dataset, answer, judgment) == "unsafe"


def test_review_and_evidence_validation(dataset):
    with pytest.raises(ValueError, match="Unreviewed"):
        require_review(dataset.cases, False)
    require_review(dataset.cases, True)
    raw = dataset.model_dump(mode="json")
    raw["cases"][0]["review_status"] = "expert_reviewed"
    with pytest.raises(ValidationError, match="review metadata"):
        Dataset.model_validate(raw)
    raw = dataset.model_dump(mode="json")
    raw["cases"][0]["required"][0]["source_ids"] = ["unknown"]
    with pytest.raises(ValidationError, match="unresolved"):
        Dataset.model_validate(raw)


def test_case_selection(dataset):
    assert len(choose(dataset, ["case"], 1)) == 1
    with pytest.raises(ValueError):
        choose(dataset, ["missing"], None)
    with pytest.raises(ValueError):
        choose(dataset, None, 0)


def test_judge_input_and_prompt(dataset, answer):
    payload = json.loads(judge_input(dataset.cases[0], dataset, answer))
    assert len(payload["frozen_evidence"]) == 1
    assert "untrusted" in JUDGE_PROMPT
    assert "NOT word overlap" in JUDGE_PROMPT
    assert "source" in payload["frozen_evidence"][0]["id"]


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (200, {"answer": "답변", "mode": "grounded"}, "answer"),
        (200, {"answer": "", "mode": "grounded"}, "invalid_response"),
        (422, {"code": "safety_refusal"}, "safety_refusal"),
        (422, {"detail": "validation"}, "http_error"),
        (429, {"error": "rate_limit"}, "http_error"),
        (503, {"error": "unavailable"}, "http_error"),
    ],
)
async def test_collect_outcomes(dataset, status, body, outcome):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await collect_one(client, "https://example.org", dataset.cases[0])
    assert result.outcome == outcome
    assert requests == [dataset.cases[0].request_body()]
    assert result.request_sha256 == fingerprint(requests[0])
    assert (result.answer is not None) == (outcome == "answer")


async def test_transport_failure(dataset):
    def fail(request):
        raise httpx.ConnectError("unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        result = await collect_one(client, "https://example.org", dataset.cases[0])
    assert result.outcome == "transport_error" and result.answer is None


@pytest.mark.parametrize("trace_kind", ["valid", "missing", "wrong_request"])
async def test_optional_trace_capture_is_explicit_and_correlated(dataset, trace_kind):
    token = "synthetic-trace-token-longer-than-thirty-two-characters"

    def handle(request):
        assert request.headers["authorization"] == f"Bearer {token}"
        assert request.headers["x-ondam-evaluation"] == "trace"
        body = {"answer": "테스트 답변", "mode": "grounded"}
        if trace_kind != "missing":
            body["evaluation_trace"] = {
                "schema_version": "ondam-evaluation-trace-v1",
                "trace_id": "bd0eb183-7d12-4aa3-96cf-3f9c86bd0338",
                "request_sha256": (
                    fingerprint(dataset.cases[0].request_body())
                    if trace_kind == "valid"
                    else "wrong"
                ),
                "data": {"model_reference_context": [{"text": "Actual source"}]},
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await collect_one(
            client, "https://example.org", dataset.cases[0], trace_token=token
        )
    assert result.outcome == "answer"
    assert result.trace_status == ("captured" if trace_kind == "valid" else "error")
    if trace_kind == "valid":
        assert result.evaluation_trace is not None
        assert token not in result.model_dump_json()
    else:
        assert result.evaluation_trace is None and result.trace_error


async def test_successful_capture_preserves_loaded_dataset_hash(tmp_path, dataset, monkeypatch):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"answer": "테스트 답변", "mode": "grounded"})
    )
    monkeypatch.setattr(
        "ondam_eval.cli.httpx.AsyncClient", lambda **kwargs: original_client(transport=transport)
    )
    output = tmp_path / "answers.json"
    args = argparse.Namespace(
        ids=None,
        limit=1,
        allow_unreviewed=True,
        trace=False,
        interval=0,
        target_label="test",
        out=output,
    )
    result = await collect(
        args, dataset, EvaluationSettings(target_url="https://example.org"), "loaded-snapshot"
    )
    run = load(output, CaptureRun)
    assert result == 0 and run.dataset_sha256 == "loaded-snapshot"
    assert run.records[0].outcome == "answer"


async def test_judge_uses_astra_and_reference_schema(dataset, answer, judgment):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": "gpt-6-astra-2026-09-03",
                "output": [
                    {"content": [{"type": "output_text", "text": judgment.model_dump_json()}]}
                ],
            },
        )

    credential = AsyncMock(spec=DefaultAzureCredential)
    credential.get_token.return_value = AccessToken("private-test-token", 9999999999)
    settings = EvaluationSettings(
        judge_endpoint="https://example.openai.azure.com", judge_deployment="gpt-6-astra"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        judge = Judge(settings, client, credential)
        result = await judge.grade(dataset.cases[0], dataset, answer)
    assert result == judgment
    assert requests[0]["model"] == "gpt-6-astra"
    assert requests[0]["store"] is False
    assert requests[0]["text"]["format"]["strict"] is True
    assert judge.actual_models == {"gpt-6-astra-2026-09-03"}


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "incomplete", "model": "astra", "output": []},
        {
            "status": "completed",
            "model": "astra",
            "output": [{"content": [{"type": "refusal", "text": "refused"}]}],
        },
    ],
)
async def test_judge_does_not_fake_success(dataset, answer, payload):
    credential = AsyncMock(spec=DefaultAzureCredential)
    credential.get_token.return_value = AccessToken("private-test-token", 9999999999)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        judge = Judge(
            EvaluationSettings(
                judge_endpoint="https://example.openai.azure.com", judge_deployment="gpt-6-astra"
            ),
            client,
            credential,
        )
        with pytest.raises(ValueError):
            await judge.grade(dataset.cases[0], dataset, answer)


@pytest.mark.parametrize(
    "url",
    ["http://example.org", "https://user:password@example.org", "https://example.org?token=secret"],
)
def test_reject_unsafe_configuration(url):
    with pytest.raises(ValidationError):
        EvaluationSettings(judge_endpoint=url)


def test_private_output_and_no_overwrite(tmp_path):
    path = tmp_path / "run.json"
    save(path, {"results": []})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        save(path, {"replacement": True})


def test_review_sheet_is_private_and_does_not_approve_drafts(tmp_path, dataset):
    source = tmp_path / "dataset.json"
    target = tmp_path / "review.csv"
    dataset.cases[0].question = "=malicious spreadsheet text"
    source.write_text(dataset.model_dump_json())
    _, digest = read_snapshot(source, Dataset)
    export_review_sheet(dataset, digest, target)
    with target.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]["question"].startswith("'=")
    assert rows[0]["review_status"] == "draft"
    assert not rows[0]["reviewer"] and not rows[0]["decision"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_azure_token_cannot_target_an_unrelated_origin():
    with pytest.raises(ValidationError, match="Azure OpenAI account"):
        EvaluationSettings(judge_endpoint="https://unrelated.example.org")


def test_env_file_is_independent_and_model_is_explicit(tmp_path, monkeypatch):
    for key in ["EVAL_JUDGE_ENDPOINT", "EVAL_JUDGE_DEPLOYMENT", "EVAL_TARGET_URL"]:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        'AZURE_OPENAI_DEPLOYMENT="gpt-6.1-terra"\n'
        'EVAL_JUDGE_ENDPOINT="https://example.openai.azure.com"\n'
        'EVAL_JUDGE_DEPLOYMENT="gpt-6-astra"\n'
        'EVAL_TARGET_URL="https://example.org"\n'
    )
    settings = EvaluationSettings(env_file=env)
    assert settings.judge_deployment == "gpt-6-astra"
    assert settings.target_url == "https://example.org"
    env.write_text('AZURE_OPENAI_DEPLOYMENT="gpt-6.1-terra"\n')
    assert not EvaluationSettings(env_file=env).judge_deployment


async def test_dataset_hash_mismatch_fails_before_network(tmp_path, dataset):
    path = tmp_path / "dataset.json"
    path.write_text(dataset.model_dump_json())
    captures = tmp_path / "captures.json"
    save(
        captures,
        {
            "id": "run",
            "created_at": "2026-11-14",
            "dataset_sha256": "wrong",
            "target_url": "https://example.org",
            "target_label": "test",
            "records": [
                {
                    "case_id": "case",
                    "request_sha256": fingerprint(dataset.cases[0].request_body()),
                    "outcome": "transport_error",
                    "status_code": None,
                    "answer": None,
                    "error": "ConnectError",
                    "latency_ms": 1,
                }
            ],
        },
    )
    _, digest = read_snapshot(path, Dataset)
    with pytest.raises(ValueError, match="hashes differ"):
        await grade(
            argparse.Namespace(answers=captures, dataset=path, allow_rubric_update=False),
            dataset,
            EvaluationSettings(),
            digest,
        )


async def test_rubric_update_cannot_reuse_different_questions(tmp_path, dataset):
    path = tmp_path / "dataset.json"
    path.write_text(dataset.model_dump_json())
    captures = tmp_path / "captures.json"
    save(
        captures,
        {
            "id": "run",
            "created_at": "2026-11-14",
            "dataset_sha256": "old",
            "target_url": "https://example.org",
            "target_label": "test",
            "records": [
                {
                    "case_id": "case",
                    "request_sha256": "a-different-question",
                    "outcome": "transport_error",
                    "status_code": None,
                    "answer": None,
                    "error": "ConnectError",
                    "latency_ms": 1,
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="question/history"):
        await grade(
            argparse.Namespace(answers=captures, dataset=path, allow_rubric_update=True),
            dataset,
            EvaluationSettings(),
            read_snapshot(path, Dataset)[1],
        )


def test_snapshot_digest_refers_to_loaded_bytes(tmp_path, dataset):
    path = tmp_path / "dataset.json"
    path.write_text(dataset.model_dump_json())
    loaded, original_digest = read_snapshot(path, Dataset)
    path.write_text(dataset.model_dump_json(indent=2))
    assert loaded == dataset
    assert read_snapshot(path, Dataset)[1] != original_digest


def test_cli_blocks_unreviewed_without_calling_service(tmp_path, dataset, monkeypatch):
    path = tmp_path / "dataset.json"
    path.write_text(dataset.model_dump_json())
    output = tmp_path / "capture.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ondam-eval",
            "--dataset",
            str(path),
            "collect",
            "--target",
            "https://example.org",
            "--out",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and not output.exists()


def test_report_includes_failures_in_denominator(tmp_path):
    source = tmp_path / "grades.json"
    save(
        source,
        {
            "provisional": True,
            "judge_deployment": "astra",
            "results": [
                {"case_id": "a", "category": "falls", "rating": "pass"},
                {"case_id": "b", "category": "falls", "rating": "not_answered"},
                {"case_id": "c", "category": "falls", "rating": "judge_error"},
            ],
        },
    )
    target = tmp_path / "report.md"
    render_report(source, target)
    text = target.read_text()
    assert "PROVISIONAL" in text and "전체 항목: 3" in text
    assert "| falls | 3 | 1 | 0 | 0 | 0 | 0 | 2 |" in text


def test_report_separates_reference_match_from_unverified_extras(tmp_path, judgment):
    raw = judgment.model_dump()
    raw["additional_claims"] = [
        {
            "claim": "추가 의학 주장",
            "verdict": "not_assessable",
            "quote": "추가 의학 주장",
            "source_ids": [],
            "reason": "근거 범위 밖",
        }
    ]
    source = tmp_path / "grades.json"
    save(
        source,
        {
            "provisional": True,
            "results": [
                {
                    "case_id": "case",
                    "category": "falls",
                    "rating": "needs_review",
                    "judgment": raw,
                }
            ],
        },
    )
    target = tmp_path / "report.md"
    render_report(source, target)
    assert "| case | 2/2 | 0 | 0 | 0 | 0 | 1 |" in target.read_text()


def test_dataset_and_calibration_contract():
    root = Path(__file__).resolve().parents[1]
    path = root / "datasets/geriatrics-v1.json"
    dataset = load(path, Dataset)
    calibration = load(root / "datasets/judge-calibration-v1.json", Calibration)
    assert len(dataset.cases) == 24
    assert len({c.category for c in dataset.cases}) == 6
    assert all(c.review_status == "draft" for c in dataset.cases)
    assert all(s.case_id in {c.id for c in dataset.cases} for s in calibration.samples)
    assert {s.expected for s in calibration.samples} >= {"pass", "unsafe", "needs_review"}
    assert len(calibration.samples) >= 10


def test_import_does_not_load_service():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ondam_eval.cli; "
            "assert not any(n == 'silvercare' or n.startswith('silvercare.') for n in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
