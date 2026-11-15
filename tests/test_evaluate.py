import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from silvercare import evaluate
from silvercare.azure import UpstreamError
from silvercare.models import ChatRequest, ChatResponse, Source
from tests.test_azure import make_services


def test_medical_targets_can_require_an_exact_provenance_url() -> None:
    target = {"source_urls": ["https://medlineplus.gov/pressuresores.html"]}
    assert evaluate.relevant(
        {"source_record_id": "371", "source_url": target["source_urls"][0]}, target
    )
    assert not evaluate.relevant(
        {"source_record_id": "371", "source_url": "https://example.test/unrelated"}, target
    )


async def test_model_only_retrieval_is_explicitly_not_applicable(tmp_path):
    source = tmp_path / "retrieval.json"
    source.write_text(json.dumps({"queries": [{"query": "건강 안내"}]}))
    service = make_services(lambda _: pytest.fail("No network for no-RAG retrieval"))
    service.settings.service_variant = "local-only"
    service.search = AsyncMock(side_effect=AssertionError("No hidden retrieval"))
    report = await evaluate.evaluate(source, tmp_path / "result.json", services=service)
    assert report["status"] == "not_applicable" and report["queries"] == []
    service.search.assert_not_awaited()


async def test_retrieval_keeps_failures_and_finishes_every_query(tmp_path):
    source = tmp_path / "retrieval.json"
    source.write_text(json.dumps({"queries": [{"query": "질문 하나"}, {"query": "질문 둘"}]}))
    service = make_services(lambda _: pytest.fail("No network in injected failure test"))
    service.search = AsyncMock(side_effect=UpstreamError("search", 503))
    report = await evaluate.evaluate(source, tmp_path / "result.json", services=service)
    assert len(report["queries"]) == 2
    assert report["metrics"]["hybrid"]["error_count"] == 2
    assert report["metrics"]["keyword"]["error_count"] == 2
    assert report["nonregression_passed"] is False


@pytest.mark.parametrize("answer,passed", [("건강 정보를 안내합니다.", True), ("금지된 말", False)])
async def test_no_rag_skips_only_evidence_checks_not_content_checks(
    tmp_path, monkeypatch, answer, passed
):
    monkeypatch.chdir(tmp_path)
    corpus = tmp_path / "data/processed/expanded"
    corpus.mkdir(parents=True)
    for name in ("knowledge.jsonl", "cases.jsonl"):
        (corpus / name).write_text("{}\n")
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "no-rag",
                        "request": {"message": "안내해주세요."},
                        "forbidden": ["금지된 말"],
                        "required_source_url": "https://example.test/guide",
                        "expected_mode": "grounded",
                        "required_model_source_urls": ["https://example.test/guide"],
                    }
                ]
            }
        )
    )
    service = make_services(lambda _: pytest.fail("Use captured response"))
    service.settings.service_variant = "local-only"
    service.chat = AsyncMock(
        return_value=ChatResponse(
            answer=answer,
            sources=[],
            mode="conversation",
            notice="모델 지식 기반",
        )
    )
    report = await evaluate.evaluate_audience(scenarios, tmp_path / "result.json", services=service)
    assert report["retrieval_checks_applicable"] is False
    assert report["cases"][0]["passed"] is passed
    assert report["cases"][0]["seconds"] >= 0


async def test_required_retrieval_target_failure_blocks_even_equal_keyword_and_hybrid(
    tmp_path, monkeypatch
):
    path = tmp_path / "retrieval.json"
    path.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "query": "욕창을 예방하려면 어떻게 하나요?",
                        "source_urls": ["https://medlineplus.gov/pressuresores.html"],
                        "required": True,
                    }
                ]
            }
        )
    )

    def handler(request):
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 512}]})
        return httpx.Response(200, json={"value": []})

    service = make_services(handler)
    service.settings.azure_embedding_deployment = "test-embedding"
    monkeypatch.setattr(evaluate, "AzureServices", lambda _: service)
    report = await evaluate.evaluate(path, tmp_path / "report.json")
    assert report["metrics"]["keyword"]["hit_at_6"] == 0
    assert report["metrics"]["hybrid"]["hit_at_6"] == 0
    assert report["required_targets_passed"] is False
    assert report["nonregression_passed"] is False


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


@pytest.mark.parametrize("concepts", [[[]], [[""]], [["약사", " "]]])
def test_required_medical_concepts_reject_empty_checks(concepts):
    with pytest.raises(ValidationError, match="nonempty alternatives"):
        evaluate.AudienceCase(
            id="medical",
            request=ChatRequest(message="약을 안전하게 관리하려면 어떻게 하나요?"),
            required_concepts=concepts,
        )


def test_audience_length_limit_is_optional_and_explicit_limits_must_be_positive():
    case = evaluate.AudienceCase(id="detail", request=ChatRequest(message="자세히 알려주세요."))
    assert case.max_characters is None
    with pytest.raises(ValidationError):
        evaluate.AudienceCase(id="invalid", request=case.request, max_characters=0)


@pytest.mark.parametrize(
    "limit,length,passed",
    [(None, 900, True), (750, 750, True), (750, 751, False), (1200, 900, True)],
)
async def test_audience_only_applies_scenario_specific_length_limits(
    tmp_path, monkeypatch, limit, length, passed
):
    monkeypatch.chdir(tmp_path)
    corpus = tmp_path / "data/processed/expanded"
    corpus.mkdir(parents=True)
    for name in ("knowledge.jsonl", "cases.jsonl"):
        (corpus / name).write_text("{}\n")
    path = tmp_path / "scenarios.json"
    scenario = {
        "id": "detail",
        "request": {"message": "신청 방법을 자세히 알려주세요."},
    }
    if limit is not None:
        scenario["max_characters"] = limit
    path.write_text(json.dumps({"cases": [scenario]}))
    answer = "가" * length

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
    monkeypatch.setattr(evaluate, "AzureServices", lambda _: service)
    report = await evaluate.evaluate_audience(path, tmp_path / "report.json")
    row = report["cases"][0]
    assert row["passed"] is passed
    assert row["violations"] == ([] if passed else ["answer_too_long"])
    assert row["answer"] == answer


@pytest.mark.parametrize(
    "answer,violations",
    [
        ("약 목록을 작성하고 의사나 약사에게 확인하세요.", []),
        ("약 목록을 작성하세요.", ["missing_concept:1"]),
        ("약사에게 확인하세요.", ["missing_concept:0"]),
    ],
)
async def test_medical_evaluation_requires_every_concept_group(
    tmp_path, monkeypatch, answer, violations
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
                        "id": "medical",
                        "request": {"message": "약을 안전하게 관리하려면 어떻게 하나요?"},
                        "required_concepts": [["목록", "리스트"], ["의사", "약사"]],
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
    report = await evaluate.evaluate_audience(path, tmp_path / "report.json")
    assert report["cases"][0]["violations"] == violations
    assert report["passed"] is (not violations)


@pytest.mark.parametrize(
    "accept_refusal,error_code,expected",
    [
        (False, "content_filter", False),
        (True, "content_filter", True),
        (True, "invalid_request_error", False),
    ],
)
async def test_audience_keeps_upstream_outcomes_without_counting_refusals_as_answers(
    tmp_path, monkeypatch, accept_refusal, error_code, expected
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
                        "id": identity,
                        "request": {"message": "생활 정보를 알려주세요."},
                        "accept_provider_safety_refusal": accept_refusal,
                    }
                    for identity in ("failed", "successful")
                ]
            }
        )
    )
    calls = 0

    def handler(request):
        nonlocal calls
        if "/docs/search" in str(request.url):
            return httpx.Response(200, json={"value": []})
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": {"code": error_code}})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": "무엇이 궁금하신가요?"}]}],
            },
        )

    service = make_services(handler)
    monkeypatch.setattr(evaluate, "AzureServices", lambda _: service)
    output = tmp_path / "report.json"
    report = await evaluate.evaluate_audience(path, output)
    assert report["passed"] is expected
    assert report["cases"][0]["violations"] == ([] if expected else ["upstream:model:400"])
    assert report["cases"][1]["passed"] is True
    assert report["answers_generated"] == 1
    assert report["provider_safety_refusals"] == int(expected)
    assert report["cases"][0]["answer_generated"] is False
    assert json.loads(output.read_text()) == report


@pytest.mark.parametrize(
    "required_urls,passed",
    [
        (["https://example.test/other", "https://example.test/source"], True),
        (["https://example.test/other"], False),
    ],
)
async def test_multiple_source_targets_accept_only_an_explicit_allowed_url(
    tmp_path, monkeypatch, required_urls, passed
):
    monkeypatch.chdir(tmp_path)
    corpus = tmp_path / "data/processed/expanded"
    corpus.mkdir(parents=True)
    for name in ("knowledge.jsonl", "cases.jsonl"):
        (corpus / name).write_text("{}\n")
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "source",
                        "request": {"message": "자료가 궁금해요."},
                        "required_source_any": required_urls,
                    }
                ]
            }
        )
    )

    def handler(request):
        if "/docs/search" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "title": "Synthetic",
                            "content": "Synthetic source content.",
                            "source_url": "https://example.test/source",
                            "updated_at": "2026-11-14",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": "자료 안내입니다. [1]"}]}],
            },
        )

    service = make_services(handler)
    monkeypatch.setattr(evaluate, "AzureServices", lambda _: service)
    report = await evaluate.evaluate_audience(scenarios, tmp_path / "report.json")
    assert report["passed"] is passed
    assert report["cases"][0]["violations"] == ([] if passed else ["missing_required_source"])


@pytest.mark.parametrize("patterns", [[""], ["["]])
def test_invalid_required_output_patterns_are_rejected(patterns):
    with pytest.raises(ValidationError, match="Required pattern"):
        evaluate.AudienceCase(
            id="shape",
            request=ChatRequest(message="예시를 알려주세요."),
            required_patterns=patterns,
        )


@pytest.mark.parametrize(
    "case_change,response_change,expected",
    [
        ({}, {}, []),
        ({"expected_mode": "conversation"}, {}, ["unexpected_response_mode"]),
        ({"required_source_type_groups": [["drug_label"]]}, {}, ["missing_source_type_group:0"]),
        ({"required_patterns": ["NOT IN ANSWER"]}, {}, ["missing_pattern:0"]),
        ({"require_medical_caution": True}, {}, ["missing_medical_caution"]),
        ({"require_medical_caution": True}, {"medical_caution": "의료진과 확인하세요."}, []),
    ],
)
async def test_response_contract_checks_source_mode_example_structure_and_displayed_caution(
    tmp_path, monkeypatch, case_change, response_change, expected
):
    monkeypatch.chdir(tmp_path)
    corpus = tmp_path / "data/processed/expanded"
    corpus.mkdir(parents=True)
    for name in ("knowledge.jsonl", "cases.jsonl"):
        (corpus / name).write_text("{}\n")
    case = {
        "id": "contract",
        "request": {"message": "교육에 관한 질문 예시를 알려주세요."},
        "expected_mode": "grounded",
        "required_source_type_groups": [["welfare_guidance"]],
        "required_patterns": ['[“"][^”"\\n]{8,300}\\?[”"]'],
        **case_change,
    }
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(json.dumps({"cases": [case]}))
    result = ChatResponse(
        answer="질문 예시: “교육 장소는 어디인가요?” [1]",
        sources=[
            Source(
                id=1,
                title="Synthetic",
                url="https://example.test/source",
                excerpt="Synthetic",
                updated_at="2026-11-14",
                source_type="welfare_guidance",
            )
        ],
        mode="grounded",
        notice="Synthetic",
    ).model_copy(update=response_change)
    service = make_services(lambda _: pytest.fail("No network needed for response contract tests"))
    service.chat = AsyncMock(return_value=result)
    monkeypatch.setattr(evaluate, "AzureServices", lambda _: service)
    report = await evaluate.evaluate_audience(scenarios, tmp_path / "report.json")
    assert report["cases"][0]["violations"] == expected
    assert report["passed"] is (not expected)
