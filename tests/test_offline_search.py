import json
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from silvercare.azure import UpstreamError
from silvercare.data import SearchDocument
from silvercare.expanded import holdout
from silvercare.offline_ingest import index_schema, ingest, inspect_corpus
from silvercare.offline_search import OfflineSearch, parse_filter, validate_vector


def hit(identity: str, score: float | None = 1.0, **metadata: Any) -> dict[str, Any]:
    return {
        "_id": identity,
        "_score": score,
        "_source": {
            "id": identity,
            "title": f"제목 {identity}",
            "content": f"내용 {identity}",
            **metadata,
        },
    }


def hits(*items: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"hits": list(items)},
        },
    )


def test_nested_metadata_and_escaped_strings() -> None:
    assert parse_filter(
        "(evidence_kind eq 'reference' or "
        "(evidence_kind eq 'dataset_knowledge' and review_status ne 'none')) and "
        "(parent_id eq 'a''b' and chunk_index ge 2 and chunk_index le 4)"
    ) == {
        "bool": {
            "filter": [
                {
                    "bool": {
                        "should": [
                            {"term": {"evidence_kind": "reference"}},
                            {
                                "bool": {
                                    "filter": [
                                        {"term": {"evidence_kind": "dataset_knowledge"}},
                                        {
                                            "bool": {
                                                "must_not": [{"term": {"review_status": "none"}}]
                                            }
                                        },
                                    ]
                                }
                            },
                        ],
                        "minimum_should_match": 1,
                    }
                },
                {
                    "bool": {
                        "filter": [
                            {"term": {"parent_id": "a'b"}},
                            {"range": {"chunk_index": {"gte": 2}}},
                            {"range": {"chunk_index": {"lte": 4}}},
                        ]
                    }
                },
            ]
        }
    }


def test_any_disjunction_precedence_and_numeric_operators() -> None:
    assert parse_filter(
        "domain eq 'medical' and "
        "drug_entities/any(term: (term eq 'apixaban' or term eq 'clarithromycin'))"
    ) == {
        "bool": {
            "filter": [
                {"term": {"domain": "medical"}},
                {
                    "bool": {
                        "should": [
                            {"term": {"drug_entities": "apixaban"}},
                            {"term": {"drug_entities": "clarithromycin"}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            ]
        }
    }
    assert parse_filter("chunk_index gt -1 or chunk_index lt 2") == {
        "bool": {
            "should": [
                {"range": {"chunk_index": {"gt": -1}}},
                {"range": {"chunk_index": {"lt": 2}}},
            ],
            "minimum_should_match": 1,
        }
    }
    assert parse_filter("parent_id eq null") == {
        "bool": {"must_not": [{"exists": {"field": "parent_id"}}]}
    }
    assert parse_filter("parent_id ne null") == {"exists": {"field": "parent_id"}}


@pytest.mark.parametrize(
    "expression",
    [
        "",
        " ",
        "domain eq 'medical' garbage",
        "(domain eq 'medical'",
        "domain eq 'unterminated",
        "domain in ('medical')",
        "unknown eq 'x'",
        "chunk_index eq '2'",
        "chunk_index ge 2.5",
        "domain eq 2",
        "content eq 'secret'",
        "domain eq true",
        "domain eq 'x'; match_all",
        "not domain eq 'x'",
        "drug_entities eq 'x'",
        "drug_entities/any(t: other eq 'x')",
        "drug_entities/any(t: t eq 'x' and t eq 'y')",
        "drug_entities/any(t: t ne 'x')",
        "drug_entities/any(t: t eq null)",
        "drug_entities/any()",
        "domain/any(t: t eq 'x')",
        "(" * 66 + "domain eq 'x'" + ")" * 66,
        "chunk_index ge " + "9" * 1000,
    ],
)
def test_invalid_filters_fail_explicitly(expression: str) -> None:
    with pytest.raises(ValueError):
        parse_filter(expression)


async def test_real_hybrid_requests_prefilter_rrf_and_projection() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert request.url == "http://opensearch:9200/local-knowledge-v1/_search"
        assert body["_source"] == {"excludes": ["content_vector"]}
        if "knn" in body["query"]:
            assert body["query"] == {
                "knn": {
                    "content_vector": {
                        "vector": [0.2, 0.8],
                        "k": 50,
                        "filter": {"bool": {"filter": [{"term": {"domain": "medical"}}]}},
                    }
                }
            }
            return hits(hit("b"), hit("c"), hit("d", content_vector=[1.0]))
        assert body["query"] == {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": "건강",
                            "fields": ["title", "content"],
                            "type": "best_fields",
                            "operator": "or",
                        }
                    }
                ],
                "filter": [{"term": {"domain": "medical"}}],
            }
        }
        assert body["size"] == 50
        return hits(hit("a", 900), hit("b", 50), hit("c", 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OfflineSearch(client, "http://opensearch:9200/", "").query(
            "local-knowledge-v1",
            {
                "search": "건강",
                "filter": "domain eq 'medical'",
                "select": "id,title",
                "top": 4,
                "vectorQueries": [
                    {
                        "kind": "vector",
                        "fields": "content_vector",
                        "vector": [0.2, 0.8],
                        "k": 50,
                    }
                ],
                "vectorFilterMode": "preFilter",
                "queryType": "simple",
            },
        )
    assert len(calls) == 2
    assert [row["id"] for row in result] == ["b", "c", "a", "d"]
    assert result[0]["@search.score"] == pytest.approx(1 / 62 + 1 / 61)
    assert all(set(row) == {"id", "title", "@search.score"} for row in result)


async def test_semantic_reranker_request_and_response_ranking() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/rerank":
            assert json.loads(request.content) == {
                "query": "건강",
                "texts": ["제목 a\n내용 a", "제목 b\n내용 b"],
                "truncate": False,
                "raw_scores": False,
                "return_text": False,
            }
            return httpx.Response(
                200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]
            )
        return hits(hit("a", 10), hit("b", 5))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OfflineSearch(client, "http://search", "http://reranker/").query(
            "local-knowledge-v1",
            {
                "search": "건강",
                "queryType": "semantic",
                "semanticConfiguration": "care-semantic",
                "select": "id",
                "top": 1,
            },
        )
    assert len(calls) == 2
    assert result == [{"id": "b", "@search.score": 5.0, "@search.rerankerScore": 0.9}]


async def test_hybrid_reranks_all_candidates_in_bounded_batches() -> None:
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/rerank":
            sizes.append(len(body["texts"]))
            return httpx.Response(
                200,
                json=[
                    {"index": i, "score": float(text.split()[1]) / 100}
                    for i, text in enumerate(body["texts"])
                ],
            )
        start = 50 if "knn" in body["query"] else 0
        return hits(*(hit(str(i), 1) for i in range(start, start + 50)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OfflineSearch(client, "http://search", "http://reranker").query(
            "local-knowledge-v1",
            {
                "search": "건강",
                "queryType": "semantic",
                "top": 3,
                "vectorQueries": [{"vector": [0.2, 0.8], "k": 50}],
            },
        )
    assert sizes == [64, 36]
    assert [row["id"] for row in result] == ["99", "98", "97"]


async def test_match_all_parent_lookup_keeps_order_and_never_reranks() -> None:
    calls = []
    expression = (
        "evidence_kind eq 'reference' and "
        "((parent_id eq 'p' and chunk_index ge 1 and chunk_index le 3))"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert body["query"] == {
            "bool": {"must": [{"match_all": {}}], "filter": [parse_filter(expression)]}
        }
        assert body["sort"] == [{"chunk_index": "asc"}, {"id": "asc"}]
        assert body["size"] == 18
        return hits(
            hit("b", None, chunk_index=1, parent_id="p"),
            hit("a", None, chunk_index=2, parent_id="p"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OfflineSearch(client, "http://search", "").query(
            "local-knowledge-v1",
            {
                "search": "*",
                "filter": expression,
                "orderby": "chunk_index asc",
                "top": 18,
                "queryType": "semantic",
            },
        )
    assert len(calls) == 1
    assert [row["chunk_index"] for row in result] == [1, 2]
    assert all("@search.rerankerScore" not in row for row in result)


async def test_dialogue_age_profile_is_modest_and_survives_fusion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "knn" not in body["query"]:
            scoring = body["query"]["function_score"]
            assert scoring["functions"] == [
                {
                    "filter": {"term": {"age_group": "older_adult"}},
                    "weight": 1.1,
                }
            ]
            assert scoring["query"]["bool"]["filter"] == [{"term": {"evidence_kind": "dialogue"}}]
        return hits(hit("young", age_group="young_adult"), hit("older", age_group="older_adult"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OfflineSearch(client, "http://search", "").query(
            "local-cases-v1",
            {
                "search": "이야기",
                "filter": "evidence_kind eq 'dialogue'",
                "scoringProfile": "care-age",
                "scoringParameters": ["preferredAge-older_adult"],
                "vectorQueries": [{"vector": [1.0, 0.1]}],
            },
        )
    assert [row["id"] for row in result] == ["older", "young"]
    assert result[0]["@search.score"] == pytest.approx(2 / 62 * 1.1)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"hits": []},
        {"hits": {"hits": {}}},
        {"hits": {"hits": [None]}},
        {"hits": {"hits": [{"_id": "a", "_score": 1, "_source": {}}]}},
        {"hits": {"hits": [hit("a"), hit("a")]}},
        {"timed_out": True, "hits": {"hits": []}},
        {"_shards": {"failed": 1}, "hits": {"hits": []}},
    ],
)
async def test_malformed_search_responses_are_not_empty_success(body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(UpstreamError, match="search"):
            await OfflineSearch(client, "http://search", "").query("local-test", {"search": "x"})


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        [{"index": 0, "score": 0.5}],
        [{"index": 0, "score": 0.5}, {"index": 0, "score": 0.4}],
        [{"index": -1, "score": 0.5}, {"index": 1, "score": 0.4}],
        [{"index": True, "score": 0.5}, {"index": 1, "score": 0.4}],
        [{"index": 0, "score": "0.5"}, {"index": 1, "score": 0.4}],
    ],
)
async def test_incomplete_or_malformed_reranker_fails(body: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json=body)
            if request.url.path == "/rerank"
            else hits(hit("a"), hit("b"))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="search"):
            await OfflineSearch(client, "http://search", "http://reranker").query(
                "local-test", {"search": "x", "queryType": "semantic"}
            )


@pytest.mark.parametrize(
    "payload",
    [
        {"search": "x", "filter": "unknown eq 'x'"},
        {"search": "x", "select": "id,content_vector"},
        {"search": "x", "vectorFilterMode": "postFilter"},
        {"search": "x", "queryType": "semantic"},
        {"search": "x", "queryType": []},
        {"search": "x", "orderby": "chunk_index asc"},
        {"search": "x", "top": True},
        {"search": "x", "queryType": "full"},
        {"search": "x", "vectorQueries": [{"vector": [1.0], "weight": 5}]},
        {"search": "x", "searchFields": "dataset_id"},
        {"search": "x", "unexpected": True},
        {"search": "*", "vectorQueries": [{"vector": [1.0]}]},
    ],
)
async def test_invalid_payloads_fail_before_network(payload: dict[str, Any]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid requests must not reach an upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="search"):
            await OfflineSearch(client, "http://search", "").query("local-test", payload)


async def test_empty_results_are_a_genuine_success() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return hits()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await OfflineSearch(client, "http://search", "http://reranker").query(
                "local-test", {"search": "x", "queryType": "semantic"}
            )
            == []
        )
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["http", "transport", "non-json"])
async def test_upstream_errors_are_explicit(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ConnectError("unavailable", request=request)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, content=b"not JSON")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError, match="search"):
            await OfflineSearch(client, "http://search", "").query("local-test", {"search": "x"})


@pytest.mark.parametrize("value", [[], [0.0, 0.0], [True], ["1"], [float("inf")], [float("nan")]])
def test_invalid_vectors(value: Any) -> None:
    with pytest.raises(ValueError):
        validate_vector(value)
    with pytest.raises(ValueError):
        validate_vector([1.0], 1024)


@pytest.fixture
def corpus_dir() -> Iterator[Path]:
    # Keep test-created files in the workspace, never the system temporary tree.
    directory = Path.cwd() / f".offline-test-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def document(identity: str, kind: str = "reference") -> dict[str, Any]:
    content = "공개 자료를 바탕으로 일상 건강과 생활 정보를 안내합니다."
    while holdout(content):
        content += " 함께 이야기해요."
    return {
        "id": identity,
        "title": "공개 건강 안내",
        "content": content,
        "source_url": "https://example.org/public",
        "dataset_id": "public-example",
        "evidence_kind": kind,
        "license": "CC0",
        "updated_at": "2026-01-01",
        "parent_id": "parent-a",
        "chunk_index": 1,
        "domain": "medical",
        "drug_entities": ["apixaban"],
        "audience": "provider",
        "age_group": "older_adult",
        "split_origin": "training",
    }


def write_corpora(directory: Path) -> tuple[Path, Path]:
    knowledge, cases = directory / "knowledge.jsonl", directory / "cases.jsonl"
    knowledge.write_text(json.dumps(document("k"), ensure_ascii=False) + "\n")
    cases.write_text(json.dumps(document("c", "dialogue"), ensure_ascii=False) + "\n")
    return knowledge, cases


def publication_handler(
    calls: list[httpx.Request],
    *,
    bulk: Any = None,
    embeddings: Any = None,
    count: int = 1,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "PUT":
            schema = json.loads(request.content)
            assert schema["mappings"]["dynamic"] == "strict"
            assert schema["mappings"]["properties"]["content"]["analyzer"] == "cjk"
            method = schema["mappings"]["properties"]["content_vector"]["method"]
            assert method["engine"] == "lucene"
            return httpx.Response(
                200,
                json={
                    "acknowledged": True,
                    "shards_acknowledged": True,
                    "index": path[1:],
                },
            )
        if path == "/embed":
            data = json.loads(request.content)
            assert data["truncate"] is False
            assert all(text.startswith("공개 건강 안내\n") for text in data["inputs"])
            return httpx.Response(200, json=embeddings if embeddings is not None else [[0.1, 0.9]])
        if path == "/_bulk":
            assert request.headers["Content-Type"] == "application/x-ndjson"
            assert request.content.endswith(b"\n")
            lines = [json.loads(line) for line in request.content.splitlines()]
            operation = lines[0]["create"]
            source = lines[1]
            assert operation["_id"] == source["id"]
            assert source["drug_entities"] == ["apixaban"]
            assert source["audience"] == "provider"
            assert source["content_vector"] == [0.1, 0.9]
            assert set(source) == set(SearchDocument.model_fields) | {"content_vector"}
            return httpx.Response(
                200,
                json=bulk
                if bulk is not None
                else {
                    "errors": False,
                    "items": [{"create": {**operation, "status": 201}}],
                },
            )
        if path.endswith("/_refresh"):
            return httpx.Response(200, json={"_shards": {"total": 1, "successful": 1, "failed": 0}})
        if path.endswith("/_count"):
            return httpx.Response(
                200,
                json={
                    "count": count,
                    "_shards": {"total": 1, "successful": 1, "failed": 0},
                },
            )
        pytest.fail(f"Unexpected request: {request.method} {path}")

    return handler


async def publish(
    client: httpx.AsyncClient, knowledge: Path, cases: Path, **options: Any
) -> dict[str, Any]:
    return await ingest(
        client,
        endpoint="http://opensearch",
        embedding_endpoint="http://embedding",
        knowledge=knowledge,
        cases=cases,
        knowledge_index="local-knowledge-v1",
        case_index="local-cases-v1",
        dimensions=2,
        **options,
    )


async def test_ingestion_preserves_metadata_checks_counts_and_reports(corpus_dir: Path) -> None:
    knowledge, cases = write_corpora(corpus_dir)
    calls: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(publication_handler(calls))
    ) as client:
        report = await publish(
            client,
            knowledge,
            cases,
            embedding_model="local-embedding",
            embedding_revision="pinned-revision",
        )
    assert [request.method for request in calls[:4]] == ["HEAD", "HEAD", "PUT", "PUT"]
    assert report["analyzer"] == "cjk"
    assert report["embedding"]["model"] == "local-embedding"
    assert report["embedding"]["dimensions"] == 2
    assert report["embedding"]["revision"] == "pinned-revision"
    for role, path in [("knowledge", knowledge), ("cases", cases)]:
        assert report["corpora"][role]["verified_count"] == 1
        expected_hash = inspect_corpus(path, role)["source_sha256"]
        assert report["corpora"][role]["source_sha256"] == expected_hash
    assert document("k")["content"] not in json.dumps(report, ensure_ascii=False)
    assert "provider" not in json.dumps(report)


@pytest.mark.parametrize(
    "bulk",
    [
        {},
        {"errors": False, "items": []},
        {"errors": True, "items": []},
        {
            "errors": False,
            "items": [
                {
                    "create": {
                        "_id": "wrong",
                        "_index": "local-knowledge-v1",
                        "status": 201,
                    }
                }
            ],
        },
        {
            "errors": False,
            "items": [
                {
                    "create": {
                        "_id": "k",
                        "_index": "local-knowledge-v1",
                        "status": 400,
                        "error": {},
                    }
                }
            ],
        },
        {"errors": False, "items": [{"index": {"status": 201}}]},
    ],
)
async def test_incomplete_bulk_is_not_success(corpus_dir: Path, bulk: Any) -> None:
    knowledge, cases = write_corpora(corpus_dir)
    calls: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(publication_handler(calls, bulk=bulk))
    ) as client:
        with pytest.raises(UpstreamError, match="search"):
            await publish(client, knowledge, cases)
    assert not any(request.url.path.endswith("/_count") for request in calls)


@pytest.mark.parametrize("embeddings", [[], [[0.1]], [[0.0, 0.0]], [[True, 1.0]], {"data": []}])
async def test_bad_embedding_batches_never_reach_bulk(corpus_dir: Path, embeddings: Any) -> None:
    knowledge, cases = write_corpora(corpus_dir)
    calls: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(publication_handler(calls, embeddings=embeddings))
    ) as client:
        with pytest.raises(UpstreamError, match="search"):
            await publish(client, knowledge, cases)
    assert not any(request.url.path == "/_bulk" for request in calls)


@pytest.mark.parametrize("existing", ["knowledge", "cases"])
async def test_existing_indexes_refused_before_any_mutation(
    corpus_dir: Path, existing: str
) -> None:
    knowledge, cases = write_corpora(corpus_dir)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "HEAD"
        return httpx.Response(200 if existing in request.url.path else 404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="existing index"):
            await publish(client, knowledge, cases)
    assert all(request.method == "HEAD" for request in calls)


async def test_document_count_readiness_is_enforced(corpus_dir: Path) -> None:
    knowledge, cases = write_corpora(corpus_dir)
    calls: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(publication_handler(calls, count=0))
    ) as client:
        with pytest.raises(UpstreamError, match="search"):
            await publish(client, knowledge, cases)


@pytest.mark.parametrize(
    "change",
    [
        {"split_origin": "test"},
        {"unknown_private_field": "secret"},
        {"content": ""},
        {"content": "연락처는 010-1234-5678 입니다."},
        {"source_url": "http://localhost/private"},
        {"source_url": "https://10.0.0.1/private"},
        {"source_url": "https://server.internal/private"},
        {"parent_id": ""},
        {"chunk_index": -1},
        {"evidence_kind": "dialogue"},
    ],
)
async def test_invalid_corpora_are_rejected_before_network(
    corpus_dir: Path, change: dict[str, Any]
) -> None:
    knowledge, cases = write_corpora(corpus_dir)
    knowledge.write_text(json.dumps({**document("k"), **change}, ensure_ascii=False) + "\n")

    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid corpora must be rejected before index preflight")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await publish(client, knowledge, cases)


@pytest.mark.parametrize("kind", ["dialogue", "qa_case"])
def test_case_stable_holdouts_are_not_published(corpus_dir: Path, kind: str) -> None:
    content = "보류할 공개 평가 사례입니다."
    while not holdout(content):
        content += " 함께 이야기해요."
    prepared = content if kind == "dialogue" else f"질문·선택지:\n{content}\n라벨 정답:\n정답"
    path = corpus_dir / "cases.jsonl"
    path.write_text(json.dumps({**document("case", kind), "content": prepared}, ensure_ascii=False))
    with pytest.raises(ValueError, match="holdout"):
        inspect_corpus(path, "cases")


def test_ingestion_retains_only_prepared_official_guide_landlines(corpus_dir: Path) -> None:
    path = corpus_dir / "knowledge.jsonl"
    row = {
        **document("guide"),
        "dataset_id": "mohw-welfare-senior-2026",
        "content": "공식기관 문의(☎02-123-4567)",
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    assert inspect_corpus(path, "knowledge")["documents"] == 1
    for change in (
        {"dataset_id": "other"},
        {"content": "개인 문의(☎010-1234-5678)"},
        {"content": "표시 없는 번호 02-123-4567"},
    ):
        path.write_text(json.dumps({**row, **change}, ensure_ascii=False) + "\n")
        with pytest.raises(ValueError, match="unredacted"):
            inspect_corpus(path, "knowledge")


def test_mapping_has_exact_metadata_fields_and_configurable_dimensions() -> None:
    schema = index_schema(1024)
    properties = schema["mappings"]["properties"]
    assert properties["content_vector"]["dimension"] == 1024
    assert properties["chunk_index"] == {"type": "integer"}
    assert properties["domain"] == properties["drug_entities"] == {"type": "keyword"}
    assert set(properties) == set(SearchDocument.model_fields) | {"content_vector"}
    with pytest.raises(ValueError):
        index_schema(0)
