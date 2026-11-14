import json

import httpx
import pytest

from silvercare.azure import UpstreamError
from silvercare.data import SearchDocument
from silvercare.expanded import annotate_documents
from silvercare.models import ChatRequest
from silvercare.retrieval import (
    choose_parents,
    conversation_only,
    domain_filter,
    merge_parent,
    preferred_filter,
    retrieval_plan,
    source_profile,
    terminology,
)
from tests.test_azure import make_services


def row(identity, parent, index=0, content="Synthetic reference text.", **extra):
    return {
        "id": identity,
        "parent_id": parent,
        "chunk_index": index,
        "title": "Synthetic",
        "content": content,
        "dataset_id": "synthetic",
        "source_record_id": f"{parent}:chunk-{index}",
        "source_url": "https://example.test/source",
        "updated_at": "2026-11-14",
        "source_type": "patient_education",
        "source_language": "en",
        "jurisdiction": "US",
        "domain": "medical",
        "evidence_kind": "reference",
        **extra,
    }


@pytest.mark.parametrize(
    "question,domains",
    [
        ("디지털배움터 신청 전화번호만 알려주세요.", ("welfare",)),
        ("가바펜틴 처방 전에 콩팥 기능을 왜 확인하나요?", ("medical",)),
        ("당뇨 진료비 지원금 신청 방법도 궁금해요.", ("medical", "welfare")),
        ("사레가 잘 들어요", ("medical",)),
        ("Tell me about pressure injury dressing evidence", ("medical",)),
        ("다른 주제에 관해 궁금해요", ()),
    ],
)
def test_conservative_domain_selection_preserves_mixed_and_unknown_intents(question, domains):
    assert retrieval_plan(question).domains == domains
    if len(domains) != 1:
        assert "domain eq" not in domain_filter(retrieval_plan(question))


def test_explicit_chat_skips_factual_retrieval_but_a_factual_question_does_not():
    assert conversation_only("오늘 친구와 산책해서 기분이 좋아요. 그냥 이야기 나누고 싶어요.")
    assert conversation_only("오늘 친구들과 즐거웠어요. 같이 이야기하고 싶어요.")
    assert conversation_only("안녕하세요!")
    assert not conversation_only("안녕하세요. 당뇨가 무엇인지 알려주세요.")
    assert not conversation_only("이야기하고 싶어요. 낙상을 예방하는 방법도 알려주세요.")
    assert not retrieval_plan("그냥 이야기 나누고 싶어요.").search_references


def test_drug_identity_comes_from_the_current_question_not_generated_search_terms():
    plan = retrieval_plan(
        "약을 함께 먹어도 되나요?\napixaban renal dose",
        "약을 함께 먹어도 되나요?",
    )
    assert plan.drugs == ()
    assert plan.needs_drug_identity
    assert "source_type ne 'drug_label'" in domain_filter(plan)
    assert "drug_label" not in preferred_filter(plan)
    known = retrieval_plan("엘리퀴스는 신장 기능을 왜 확인하나요?")
    assert known.drugs == ("apixaban",)
    assert "term eq 'apixaban'" in domain_filter(known)
    assert "drug_label" in preferred_filter(known)
    mixed = retrieval_plan("엘리퀴스 약값 지원금 신청은 어떻게 하나요?")
    assert "welfare_guidance" in preferred_filter(mixed)
    assert not retrieval_plan("가바펜틴 처방 시 무엇을 확인하나요?").qa_examples
    assert retrieval_plan("의학 시험의 객관식 문항이 궁금해요").qa_examples


def test_provenance_annotation_does_not_turn_source_location_into_korean_approval():
    documents = [
        SearchDocument(
            id=f"doc-{i}",
            title=f"미국 의약품 라벨 · {name}",
            content="Synthetic complete source paragraph.",
            source_url="https://example.test/shared-table",
            dataset_id="openfda-drug-labels",
            evidence_kind="reference",
            license="CC0",
            updated_at="2026-11-14",
            source_record_id=f"label-{i}:drug_interactions:chunk-0",
        )
        for i, name in enumerate(("아픽사반", "가바펜틴"))
    ]
    annotate_documents(documents)
    assert documents[0].parent_id != documents[1].parent_id
    assert all(document.domain == "medical" for document in documents)
    assert all(document.jurisdiction == "US" for document in documents)
    assert all(document.source_language == "en" for document in documents)
    assert all(document.topic for document in documents)
    assert documents[0].drug_entities == ["apixaban"]
    assert source_profile("aihub-source", "dataset_knowledge").jurisdiction == "unspecified"
    assert source_profile("new-source", "reference").domain == "general"


def test_parent_selection_deduplicates_chunks_not_distinct_drugs_at_the_same_url():
    a = row("a0", "drug-a")
    b = row("b0", "drug-b")
    result = choose_parents([a, row("a1", "drug-a", 1), b], [a, row("a2", "drug-a", 2)])
    assert [r["parent_id"] for r in result] == ["drug-a", "drug-b"]
    assert result[0]["source_url"] == result[1]["source_url"]
    welfare = row("w0", "welfare", domain="welfare")
    mixed = choose_parents([a, b, welfare], [a, b], domains=("medical", "welfare"))
    assert {r["domain"] for r in mixed[:2]} == {"medical", "welfare"}


def test_adjacent_chunks_are_ordered_and_exact_overlap_is_removed():
    shared = "This exact source overlap is deliberately longer than forty characters."
    previous = row("a0", "p", 0, "Original condition.\n" + shared)
    anchor = row("a1", "p", 1, shared + "\nOriginal qualification.")
    following = row("a2", "p", 2, "Additional source exception.")
    result = merge_parent(anchor, [following, anchor, previous])
    assert result["content"].count(shared) == 1
    assert result["content"].startswith("Original condition.")
    assert result["content"].endswith("Additional source exception.")
    assert result["context_record_ids"] == ["p:chunk-0", "p:chunk-1", "p:chunk-2"]
    assert result["source_record_id"] == "p"


def test_repeated_clinical_header_is_kept_once_without_removing_source_conditions():
    header = "Synthetic\nSource date: 2026-11-14.\nProvider-only applicability.\n"
    first = row("a0", "p", 0, header + "Condition A.")
    second = row("a1", "p", 1, header + "Exception B.")
    result = merge_parent(first, [first, second])
    assert result["content"].count(header) == 1
    assert "Condition A." in result["content"] and "Exception B." in result["content"]


def test_context_budget_omits_whole_neighbors_never_slices_a_qualification():
    previous = row("a0", "p", 0, "A" * 2000)
    anchor = row("a1", "p", 1, "B" * 2000)
    following = row("a2", "p", 2, "Complete short exception.")
    result = merge_parent(anchor, [previous, anchor, following])
    assert result["context_record_ids"] == ["p:chunk-1", "p:chunk-2"]
    assert "A" not in result["content"]
    assert result["content"].endswith("Complete short exception.")
    assert len(result["content"]) <= 3600


def test_missing_anchor_duplicate_positions_or_mixed_applicability_fail():
    anchor = row("a0", "p")
    for siblings in [
        [],
        [anchor, row("duplicate", "p")],
        [anchor, row("a1", "p", 1, jurisdiction="KR")],
        [anchor, row("a1", "p", 1, updated_at="2025-01-01")],
    ]:
        with pytest.raises(ValueError):
            merge_parent(anchor, siblings)
    with pytest.raises(ValueError, match="parent"):
        choose_parents([{"id": "missing"}], [])


def test_merged_neighbor_cannot_downgrade_clinical_risk():
    anchor = row("a0", "p", risk_level="general")
    neighbor = row("a1", "p", 1, risk_level="clinical")
    assert merge_parent(anchor, [anchor, neighbor])["risk_level"] == "clinical"


def test_terminology_is_evidence_matched_not_a_blanket_answer_replacement():
    terms = terminology([{"content": "Nonvalvular atrial fibrillation and renal impairment."}])
    assert terms["nonvalvular atrial fibrillation"] == "비판막성 심방세동"
    assert "inhibitor" not in terms


async def test_contextual_search_uses_bounded_domain_pool_and_one_adjacent_lookup():
    calls = []
    a = row("a0", "p-a", 0, "Complete source condition.")
    a1 = row("a1", "p-a", 1, "Complete source exception.")
    b = row("b0", "p-b")

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["search"] == "*":
            assert body["top"] == 18
            assert "parent_id eq 'p-a'" in body["filter"]
            assert "chunk_index le 1" in body["filter"]
            return httpx.Response(200, json={"value": [a1, a, b]})
        assert "domain eq 'medical'" in body["filter"]
        assert "parent_id" in body["select"]
        return httpx.Response(200, json={"value": [a, a1, b]})

    service = make_services(handler)
    service.settings.contextual_retrieval = True
    service.settings.prefer_official_references = True
    try:
        result = await service.search("근력과 재활", "reference", mode="keyword")
        assert len(calls) == 3
        assert len(result) == 2
        assert result[0]["context_record_ids"] == ["p-a:chunk-0", "p-a:chunk-1"]
        assert "Complete source exception." in result[0]["content"]
    finally:
        await service.close()


async def test_failed_adjacent_lookup_is_not_a_success_shaped_anchor_fallback():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"value": [] if body["search"] == "*" else [row("anchor", "p")]}
        )

    service = make_services(handler)
    service.settings.contextual_retrieval = True
    try:
        with pytest.raises(UpstreamError):
            await service.search("건강 정보", "reference")
    finally:
        await service.close()


async def test_explicit_smalltalk_calls_only_the_answer_model_not_search_or_rewrite():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path.endswith("/responses")
        context = json.loads(body["instructions"].split("\n검색 데이터(JSON):\n")[1])
        assert context["retrieval_scope"]["conversation_only"]
        assert context["reference"] == context["dialogue"] == context["qa_cases"] == []
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": "친구분과 즐거운 시간을 보내셨군요."}
                        ]
                    }
                ],
            },
        )

    service = make_services(handler)
    service.settings.contextual_retrieval = True
    service.settings.retrieval_mode = "hybrid"
    service.settings.rewrite_queries = True
    try:
        result = await service.chat(
            ChatRequest(message="오늘 친구와 산책해서 기분이 좋아요. 그냥 이야기 나누고 싶어요.")
        )
        assert len(calls) == 1
        assert result.mode == "conversation" and not result.sources
    finally:
        await service.close()
