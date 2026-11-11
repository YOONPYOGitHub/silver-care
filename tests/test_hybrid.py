import io
import json
import tarfile
import zipfile

import httpx
import pytest

from silvercare import expanded
from silvercare.azure import UpstreamError, case_question
from silvercare.corpus import CorpusCatalog
from silvercare.data import SearchDocument, care_audience, index_schema
from silvercare.expanded import KNOWLEDGE_INDEX, EmbeddingCache, case_documents, holdout, upload
from silvercare.models import ChatRequest, Message
from tests.test_azure import make_services


async def test_hybrid_search_combines_keyword_and_vector_with_filters():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.1] * 512}],
                },
            )
        assert body["search"] == "비틀거리는 부모님"
        assert len(body["vectorQueries"][0]["vector"]) == 512
        assert body["vectorQueries"][0]["k"] == 50
        assert body["vectorFilterMode"] == "preFilter"
        assert "evidence_kind" in body["filter"]
        return httpx.Response(200, json={"value": []})

    services = make_services(handler)
    services.settings.retrieval_mode = "hybrid"
    services.settings.azure_embedding_deployment = "test-embedding"
    services.settings.azure_search_case_index = "case-index"
    try:
        await services.search("비틀거리는 부모님", "reference")
        await services.search("비틀거리는 부모님", "qa_case", [0.1] * 512)
        assert len(calls) == 3
        assert "/case-index/" in calls[-1][0].url.path
        assert calls[-1][1]["filter"] == "evidence_kind eq 'qa_case'"
        assert "dataset_knowledge" in calls[1][1]["filter"]
    finally:
        await services.close()


async def test_keyword_comparison_does_not_call_embeddings():
    def handler(request):
        assert not request.url.path.endswith("/embeddings")
        assert "vectorQueries" not in json.loads(request.content)
        return httpx.Response(200, json={"value": []})

    services = make_services(handler)
    services.settings.retrieval_mode = "hybrid"
    try:
        await services.search("장기요양", "reference", mode="keyword")
    finally:
        await services.close()


@pytest.mark.parametrize(
    "data",
    [
        {"data": []},
        {"data": [{"index": 0, "embedding": [0.1] * 3}]},
        {"data": [{"index": 1, "embedding": [0.1] * 512}]},
        {"data": [{"index": 0, "embedding": ["nan"] * 512}]},
    ],
)
async def test_invalid_embedding_shape_is_not_silently_accepted(data):
    services = make_services(lambda request: httpx.Response(200, json=data))
    services.settings.azure_embedding_deployment = "test"
    try:
        with pytest.raises(UpstreamError):
            await services.embed(["test"])
    finally:
        await services.close()


def test_vector_schema_and_cache_model_dimension_isolation(tmp_path):
    schema = index_schema("test-index", 512)
    vector = next(field for field in schema["fields"] if field["name"] == "content_vector")
    assert vector["dimensions"] == 512
    assert vector["stored"] is False
    assert vector["retrievable"] is False
    assert schema["vectorSearch"]["algorithms"][0]["hnswParameters"]["metric"] == "cosine"
    path = tmp_path / "cache.sqlite3"
    cache = EmbeddingCache(path, "model-a", 512)
    cache.put("text", [0.1] * 512)
    cache.close()
    cache = EmbeddingCache(path, "model-a", 256)
    assert cache.get("text") is None
    cache.close()
    cache = EmbeddingCache(path, "model-a", 512)
    assert cache.get("text") == [0.1] * 512
    cache.close()


def test_validation_is_partitioned_not_blanket_rejected_and_negative_qa_is_not_fact(tmp_path):
    rows = [
        {
            "qa_id": i,
            "q_type": 1,
            "question": f"검사에 관한 설명 중 옳지 않은 것은? {i}",
            "answer": "틀린 선택지입니다.",
        }
        for i in range(20)
    ]
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("source.json", json.dumps(rows, ensure_ascii=False))
    raw_path = tmp_path / "source.tar"
    with tarfile.open(raw_path, "w") as archive:
        info = tarfile.TarInfo("labels.zip.part0")
        info.size = len(zipped.getvalue())
        archive.addfile(info, io.BytesIO(zipped.getvalue()))
    catalog = CorpusCatalog.model_validate(
        {
            "sources": [
                {
                    "name": "internal-medicine-validation",
                    "dataset_id": "71875",
                    "file_key": "123",
                    "purpose": "qa",
                    "raw_path": str(raw_path),
                }
            ]
        }
    )
    runtime, evaluation, counts = case_documents(catalog)
    assert runtime and evaluation
    assert len(runtime) + len(evaluation) == 20
    assert not {doc.id for doc in runtime} & {doc.id for doc in evaluation}
    assert all(doc.evidence_kind == "qa_case" for doc in runtime + evaluation)
    assert all(doc.answer_interpretation == "incorrect_choice" for doc in runtime + evaluation)
    assert all("질문·선택지" in doc.content for doc in runtime)
    assert all(doc.split_origin == "validation" for doc in runtime)
    assert counts["holdout"] == len(evaluation)
    assert holdout("동일한 질문") == holdout("동일한  질문")
    reserved = CorpusCatalog(
        sources=[catalog.sources[0].model_copy(update={"purpose": "evaluation"})]
    )
    assert case_documents(reserved) == ([], [], {})


def test_generator_receives_qa_stem_not_unverified_options_or_answer():
    content = (
        "질문·선택지:\n검사 설명 중 옳지 않은 것은?\n"
        "1) 허위 선택지\n2) 다른 선택지\n라벨 정답:\n허위 정답"
    )
    stem = case_question(content)
    assert stem == "검사 설명 중 옳지 않은 것은?"
    assert "허위" not in stem


@pytest.mark.parametrize(
    "title,expected",
    [
        ("사업관리 · 예산편성 및 집행기준", "provider"),
        ("전담사회복지사 교육", "provider"),
        ("이용자 신청 방법", "self"),
        ("가족 돌봄 안내", "caregiver"),
        ("서비스 제공 절차", "general"),
    ],
)
def test_manual_reader_tags_are_conservative(title, expected):
    assert care_audience(title) == expected


def test_dialogue_preserves_verified_age_codes_and_holdouts(tmp_path):
    rows = [
        {
            "profile": {"persona": {"human": [code, "G01"]}},
            "talk": {
                "id": {"talk-id": f"{code}-{i}"},
                "content": {"HS01": f"오늘의 일상에 대해 이야기하고 싶어요. {code} {i}"},
            },
        }
        for code in ("A01", "A02", "A03", "A04")
        for i in range(10)
    ]
    path = tmp_path / "dialogue.json"
    path.write_text(json.dumps(rows, ensure_ascii=False))
    catalog = CorpusCatalog.model_validate(
        {
            "sources": [
                {
                    "name": "emotion-validation",
                    "dataset_id": "86",
                    "file_key": "66048",
                    "purpose": "dialogue",
                    "raw_path": str(path),
                }
            ]
        }
    )
    runtime, evaluation, _ = case_documents(catalog)
    assert len(runtime) + len(evaluation) == 40
    assert {d.age_group for d in runtime} == {
        "adolescent",
        "young_adult",
        "middle_adult",
        "older_adult",
    }
    assert all(d.age_group == "older_adult" for d in runtime if d.age_code == "A04")
    assert not {d.id for d in runtime} & {d.id for d in evaluation}


async def test_dialogue_age_preference_does_not_filter_out_other_generations():
    def handler(request):
        body = json.loads(request.content)
        assert body["filter"] == "evidence_kind eq 'dialogue'"
        assert body["scoringProfile"] == "care-age"
        assert body["scoringParameters"] == ["preferredAge-older_adult"]
        return httpx.Response(200, json={"value": []})

    services = make_services(handler)
    try:
        await services.search("오늘 기분을 나누고 싶어요", "dialogue")
    finally:
        await services.close()


async def test_incremental_publish_skips_unchanged_and_deletes_only_stale_staging_ids(tmp_path):
    def document(key, text):
        return SearchDocument(
            id=key,
            title="Test",
            content=text,
            source_url="https://example.org/source",
            dataset_id="test",
            evidence_kind="reference",
            license="test",
            updated_at="2026-11-09",
        )

    unchanged = document("one", "unchanged")
    desired = document("two", "updated")
    old = document("two", "old")
    stale = document("stale", "stale")
    path = tmp_path / "source.jsonl"
    path.write_text(unchanged.model_dump_json() + "\n" + desired.model_dump_json() + "\n")
    actions = []

    def handler(request):
        if request.method == "PUT":
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "value": [row.model_dump() for row in (unchanged, old, stale)],
                },
            )
        actions.extend(body["value"])
        return httpx.Response(
            200,
            json={
                "value": [{"status": True} for _ in body["value"]],
            },
        )

    services = make_services(handler)
    cache = EmbeddingCache(tmp_path / "cache.sqlite3", "test", 512)
    cache.put("Test\nupdated", [0.1] * 512)
    try:
        result = await upload(services, cache, KNOWLEDGE_INDEX, path)
        assert result["unchanged"] == 1
        assert result["uploaded_changes"] == 1
        assert result["new_embeddings"] == 0
        assert result["deleted_stale"] == 1
        assert [(a["@search.action"], a["id"]) for a in actions] == [
            ("mergeOrUpload", "two"),
            ("delete", "stale"),
        ]
    finally:
        cache.close()
        await services.close()


@pytest.mark.parametrize("kind", ["knowledge", "cases"])
async def test_publish_rejects_recorded_live_indexes_even_with_different_cli_settings(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(expanded, "OUTPUT", tmp_path)
    service = make_services(lambda _: pytest.fail("No mutations of a recorded live index"))
    (tmp_path / "release.json").write_text(
        json.dumps(
            {
                "AZURE_SEARCH_ENDPOINT": service.settings.azure_search_endpoint,
                "AZURE_SEARCH_INDEX": expanded.KNOWLEDGE_INDEX,
                "AZURE_SEARCH_CASE_INDEX": expanded.CASE_INDEX,
            }
        )
    )
    target = expanded.KNOWLEDGE_INDEX if kind == "knowledge" else expanded.CASE_INDEX
    document = SearchDocument(
        id="one",
        title="Synthetic",
        content="Synthetic reference text",
        source_url="https://example.test/reference",
        dataset_id="synthetic",
        evidence_kind="reference" if kind == "knowledge" else "dialogue",
        license="synthetic",
        updated_at="2026-11-11",
    )
    path = tmp_path / "docs.jsonl"
    path.write_text(document.model_dump_json() + "\n")
    cache = EmbeddingCache(tmp_path / "cache.sqlite3", "synthetic", 512)
    try:
        with pytest.raises(ValueError, match="live index"):
            await upload(service, cache, target, path)
    finally:
        cache.close()
        await service.close()


async def test_publish_protects_the_configured_live_case_index(tmp_path, monkeypatch):
    monkeypatch.setattr(expanded, "OUTPUT", tmp_path)
    service = make_services(lambda _: pytest.fail("No mutations of a configured live case index"))
    service.settings.azure_search_case_index = expanded.CASE_INDEX
    document = SearchDocument(
        id="one",
        title="Synthetic",
        content="Synthetic dialogue example",
        source_url="https://example.test/reference",
        dataset_id="synthetic",
        evidence_kind="dialogue",
        license="synthetic",
        updated_at="2026-11-11",
    )
    path = tmp_path / "docs.jsonl"
    path.write_text(document.model_dump_json() + "\n")
    cache = EmbeddingCache(tmp_path / "cache.sqlite3", "synthetic", 512)
    try:
        with pytest.raises(ValueError, match="live index"):
            await upload(service, cache, expanded.CASE_INDEX, path)
    finally:
        cache.close()
        await service.close()


async def test_query_normalization_redacts_input_and_has_no_medical_advice_role():
    def handler(request):
        body = json.loads(request.content)
        assert body["store"] is False
        assert "진단" in body["instructions"]
        assert "010-1234-5678" not in body["input"]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": "고령자 보행 균형 낙상 예방"}]}
                ],
            },
        )

    services = make_services(handler)
    try:
        assert (
            await services.rewrite_query("비틀거려요 010-1234-5678") == "고령자 보행 균형 낙상 예방"
        )
    finally:
        await services.close()


async def test_invalid_query_normalization_is_an_explicit_failure():
    services = make_services(
        lambda request: httpx.Response(
            200,
            json={
                "status": "incomplete",
                "output": [],
            },
        )
    )
    try:
        with pytest.raises(UpstreamError):
            await services.rewrite_query("검사 질문")
    finally:
        await services.close()


async def test_chat_rewrite_distinguishes_current_subject_from_prior_family_topic():
    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 512}]})
        if "/docs/search" in str(request.url):
            assert body["search"].startswith("이번에는 제 이야기예요.")
            if "dataset_knowledge" in body["filter"]:
                assert "친구와 일상 대화" in body["search"]
            return httpx.Response(200, json={"value": []})
        if body["max_output_tokens"] == 256:
            context = json.loads(body["input"])
            assert context["current_question"] == "이번에는 제 이야기예요. 친구들과 즐겁게 지내요."
            assert context["previous_questions"] == ["어머니 돌봄이 궁금해요."]
            text = "친구와 일상 대화"
        else:
            assert body["input"][-1]["content"].startswith("이번에는 제 이야기")
            text = "즐거운 하루를 보내셨군요."
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": text}]}],
            },
        )

    services = make_services(handler)
    services.settings.retrieval_mode = "hybrid"
    services.settings.rewrite_queries = True
    services.settings.azure_embedding_deployment = "test-embedding"
    try:
        await services.chat(
            ChatRequest(
                message="이번에는 제 이야기예요. 친구들과 즐겁게 지내요.",
                history=[Message(role="user", content="어머니 돌봄이 궁금해요.")],
            )
        )
    finally:
        await services.close()


async def test_release_rejects_failed_audience_evaluation_before_remote_calls(
    tmp_path, monkeypatch
):
    service = make_services(lambda request: pytest.fail("No release after audience regression"))
    monkeypatch.setattr(expanded, "AzureServices", lambda settings: service)
    monkeypatch.setattr(expanded, "OUTPUT", tmp_path)
    digest = "a" * 64
    (tmp_path / "publish-report.json").write_text(
        json.dumps(
            {
                "embedding_deployment": service.settings.azure_embedding_deployment,
                "dimensions": service.settings.embedding_dimensions,
                "endpoint": service.settings.azure_search_endpoint,
                "indexes": {expanded.KNOWLEDGE_INDEX: {"sha256": digest}},
            }
        )
    )
    (tmp_path / "retrieval-evaluation-final.json").write_text(
        json.dumps(
            {
                "nonregression_passed": True,
                "index": expanded.KNOWLEDGE_INDEX,
                "endpoint": service.settings.azure_search_endpoint,
                "knowledge_sha256": digest,
                "hybrid_pipeline": {"rewrite_queries": False, "semantic_ranking": False},
            }
        )
    )
    (tmp_path / "audience-evaluation.json").write_text(json.dumps({"passed": False}))
    with pytest.raises(ValueError, match="audience response evaluation"):
        await expanded.release()


async def test_release_rejects_publish_report_from_another_search_service(tmp_path, monkeypatch):
    service = make_services(lambda _: pytest.fail("No release after endpoint mismatch"))
    monkeypatch.setattr(expanded, "AzureServices", lambda settings: service)
    monkeypatch.setattr(expanded, "OUTPUT", tmp_path)
    (tmp_path / "publish-report.json").write_text(
        json.dumps(
            {
                "endpoint": "https://different-search.example",
                "embedding_deployment": service.settings.azure_embedding_deployment,
                "dimensions": service.settings.embedding_dimensions,
            }
        )
    )
    (tmp_path / "retrieval-evaluation-final.json").write_text("{}")
    (tmp_path / "audience-evaluation.json").write_text("{}")
    with pytest.raises(ValueError, match="Published search endpoint"):
        await expanded.release()


async def test_publish_checks_both_evidence_files_before_any_remote_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(expanded, "OUTPUT", tmp_path)
    service = make_services(lambda _: pytest.fail("No mutation before complete preflight"))
    service.settings.azure_embedding_deployment = "synthetic"
    monkeypatch.setattr(expanded, "AzureServices", lambda settings: service)
    document = SearchDocument(
        id="one",
        title="Synthetic",
        content="Synthetic reference text",
        source_url="https://example.test/source",
        dataset_id="synthetic",
        evidence_kind="reference",
        license="synthetic",
        updated_at="2026-11-11",
    )
    (tmp_path / "knowledge.jsonl").write_text(document.model_dump_json() + "\n")
    (tmp_path / "cases.jsonl").write_text(document.model_dump_json() + "\n")
    with pytest.raises(ValueError, match="evidence kinds"):
        await expanded.publish()
