"""Azure-shaped retrieval over private, unauthenticated OpenSearch endpoints.

The index schema is supplied by :mod:`silvercare.offline_ingest`. Hybrid search
uses two independently prefiltered candidate lists and reciprocal-rank fusion
(constant 60), rather than trying to combine incomparable BM25/cosine scores.
"""

import logging
import math
import re
from typing import Any, NoReturn
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)
KEYWORD_FIELDS = frozenset(
    """id source_url dataset_id evidence_kind license updated_at source_record_id
    source_file_key source_display source_year review_status topic risk_level
    split_origin qa_type answer_interpretation audience age_code age_group domain
    source_type source_language jurisdiction parent_id drug_entities""".split()
)
DOCUMENT_FIELDS = KEYWORD_FIELDS | {"title", "content", "chunk_index"}
_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:''|[^'])*')|"
    r"(?P<number>-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)|"
    r"(?P<word>[A-Za-z_][A-Za-z_0-9]*)|(?P<punct>[():/]))"
)


def fail(message: str, status: int = 502) -> NoReturn:
    # AzureServices imports this adapter; keep the exception import acyclic.
    from silvercare.azure import UpstreamError

    logger.warning("Offline search %s", message)
    raise UpstreamError("search", status)


def endpoint_base(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected an HTTP service base URL without credentials or query")
    return endpoint.rstrip("/")


def index_name(index: str) -> str:
    if (
        not isinstance(index, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,254}", index)
        or index in {".", ".."}
    ):
        raise ValueError("Expected an explicit lowercase index name")
    return index


def validate_vector(value: Any, dimensions: int | None = None) -> list[float]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 16000
        or (dimensions is not None and len(value) != dimensions)
        or any(not _finite_number(item) for item in value)
    ):
        raise ValueError("Expected a finite embedding with the configured dimensions")
    vector = [float(item) for item in value]
    if not any(vector):
        raise ValueError("Cosine embeddings must not be zero vectors")
    return vector


def _finite_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


async def request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        logger.warning("Offline search transport error: %s", type(exc).__name__)
        from silvercare.azure import UpstreamError

        raise UpstreamError("search") from exc
    if not response.is_success:
        fail(f"HTTP {response.status_code}", response.status_code)
    return response


def response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        fail("response is not valid JSON")


class _Filter:
    def __init__(self, expression: str) -> None:
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 16384:
            raise ValueError("Invalid OData filter")
        self.tokens: list[str] = []
        offset = 0
        while offset < len(expression.rstrip()):
            match = _TOKEN.match(expression, offset)
            if match is None:
                raise ValueError("Unsupported OData token")
            self.tokens.append(match.group().strip())
            offset = match.end()
        if len(self.tokens) > 4096:
            raise ValueError("OData filter is too complex")
        self.position = 0

    def peek(self) -> str:
        return self.tokens[self.position] if self.position < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or (expected is not None and token != expected):
            raise ValueError("Invalid or incomplete OData expression")
        self.position += 1
        return token

    def expression(
        self, depth: int = 0, variable: str | None = None, collection: str | None = None
    ) -> dict[str, Any]:
        if depth > 64:
            raise ValueError("OData filter nesting is too deep")
        clauses = [self.conjunction(depth, variable, collection)]
        while self.peek() == "or":
            self.take()
            clauses.append(self.conjunction(depth, variable, collection))
        return (
            clauses[0]
            if len(clauses) == 1
            else {"bool": {"should": clauses, "minimum_should_match": 1}}
        )

    def conjunction(
        self, depth: int, variable: str | None, collection: str | None
    ) -> dict[str, Any]:
        clauses = [self.atom(depth, variable, collection)]
        while self.peek() == "and":
            if collection is not None:
                raise ValueError("Collection any supports equality disjunctions only")
            self.take()
            clauses.append(self.atom(depth, variable, collection))
        return clauses[0] if len(clauses) == 1 else {"bool": {"filter": clauses}}

    def atom(self, depth: int, variable: str | None, collection: str | None) -> dict[str, Any]:
        if self.peek() == "(":
            self.take()
            result = self.expression(depth + 1, variable, collection)
            self.take(")")
            return result
        field = self.take()
        if variable is None and field not in DOCUMENT_FIELDS:
            raise ValueError("Unsupported OData field")
        if self.peek() == "/":
            if variable is not None or field != "drug_entities":
                raise ValueError("Unsupported OData collection")
            self.take("/")
            self.take("any")
            self.take("(")
            alias = self.take()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", alias):
                raise ValueError("Invalid OData lambda variable")
            self.take(":")
            result = self.expression(depth + 1, alias, field)
            self.take(")")
            return result
        if variable is not None:
            if field != variable or collection is None:
                raise ValueError("OData lambda must compare its bound variable")
            field = collection
        elif field == "drug_entities":
            raise ValueError("Collection filters require any")
        operator = self.take()
        if operator not in {"eq", "ne", "ge", "le", "gt", "lt"}:
            raise ValueError("Unsupported OData comparison")
        literal = self.take()
        value: str | int | float | None
        if literal.startswith("'"):
            value = literal[1:-1].replace("''", "'")
        elif literal == "null":
            value = None
        else:
            try:
                value = float(literal) if re.search(r"[.eE]", literal) else int(literal)
            except ValueError as exc:
                raise ValueError("Unsupported OData literal") from exc
            if not _finite_number(value):
                raise ValueError("Non-finite OData number")
        if field in {"title", "content"}:
            raise ValueError("Text fields are not filterable")
        if value is not None and (
            (field == "chunk_index" and not isinstance(value, int))
            or (field != "chunk_index" and not isinstance(value, str))
        ):
            raise ValueError("OData literal type does not match the field")
        if field == "drug_entities" and operator != "eq":
            raise ValueError("Collection any supports equality disjunctions only")
        if value is None:
            if operator not in {"eq", "ne"} or field == "drug_entities":
                raise ValueError("Unsupported null comparison")
            exists = {"exists": {"field": field}}
            return {"bool": {"must_not": [exists]}} if operator == "eq" else exists
        if operator in {"eq", "ne"}:
            term = {"term": {field: value}}
            return {"bool": {"must_not": [term]}} if operator == "ne" else term
        return {"range": {field: {{"ge": "gte", "le": "lte"}.get(operator, operator): value}}}


def parse_filter(expression: str) -> dict[str, Any]:
    """Compile the supported OData subset; never silently ignore a predicate."""
    parser = _Filter(expression)
    result = parser.expression()
    if parser.peek():
        raise ValueError("Trailing OData input")
    return result


def _integer(value: Any, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError("Search size must be a positive bounded integer")
    return int(value)


def _fields(value: Any) -> list[str]:
    if not isinstance(value, str):
        raise ValueError("Expected comma-separated field names")
    fields = [field.strip() for field in value.split(",")]
    if not fields or any(field not in DOCUMENT_FIELDS for field in fields):
        raise ValueError("Unsupported search field")
    return fields


def _hits(data: Any) -> list[tuple[str, dict[str, Any], float]]:
    if not isinstance(data, dict) or data.get("timed_out", False) is not False:
        fail("incomplete search response")
    shards = data.get("_shards")
    if shards is not None and (
        not isinstance(shards, dict)
        or type(shards.get("failed")) is not int
        or shards["failed"] != 0
    ):
        fail("failed search shards")
    hits = data.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        fail("malformed search hits")
    result = []
    seen = set()
    for hit in hits["hits"]:
        if not isinstance(hit, dict):
            fail("malformed search hit")
        identity, source, score = hit.get("_id"), hit.get("_source"), hit.get("_score")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in seen
            or not isinstance(source, dict)
            or source.get("id") != identity
            or "_score" not in hit
            or (score is not None and not _finite_number(score))
        ):
            fail("malformed search document")
        seen.add(identity)
        result.append((identity, source, float(score) if score is not None else 0.0))
    return result


class OfflineSearch:
    def __init__(
        self, client: httpx.AsyncClient, endpoint: str, reranker_endpoint: str = ""
    ) -> None:
        self.client = client
        self.endpoint = endpoint_base(endpoint)
        self.reranker_endpoint = endpoint_base(reranker_endpoint) if reranker_endpoint else ""

    async def query(self, index: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return await self._query(index, payload)
        except ValueError:
            fail("invalid request or upstream values")

    async def _query(self, index: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        index_name(index)
        if not isinstance(payload, dict) or payload.keys() - {
            "search",
            "filter",
            "select",
            "top",
            "orderby",
            "vectorQueries",
            "queryType",
            "searchMode",
            "searchFields",
            "scoringProfile",
            "scoringParameters",
            "vectorFilterMode",
            "semanticConfiguration",
        }:
            raise ValueError("Unsupported search payload")
        text = payload.get("search", "*")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Expected nonempty search text")
        match_all = text == "*"
        top = _integer(payload.get("top", 50), 1000)
        selected = _fields(payload["select"]) if "select" in payload else None
        fields = _fields(payload.get("searchFields", "title,content"))
        if any(field not in {"title", "content"} for field in fields):
            raise ValueError("Only title/content support text search")
        query_type = payload.get("queryType", "simple")
        if query_type not in ("simple", "semantic"):
            raise ValueError("Unsupported queryType")
        if payload.get("searchMode", "any") not in ("any", "all"):
            raise ValueError("Unsupported searchMode")
        if payload.get("vectorFilterMode", "preFilter") != "preFilter":
            raise ValueError("Only vector prefilters are supported")
        if payload.get("semanticConfiguration", "care-semantic") != "care-semantic":
            raise ValueError("Unsupported semantic configuration")
        if "orderby" in payload and payload["orderby"] != "chunk_index asc":
            raise ValueError("Unsupported sort")
        ordered = "orderby" in payload
        if ordered and (not match_all or payload.get("vectorQueries")):
            raise ValueError("Chunk ordering is reserved for match-all parent lookups")
        age = payload.get("scoringProfile") == "care-age"
        if "scoringProfile" in payload and not age:
            raise ValueError("Unsupported scoring profile")
        if "scoringParameters" in payload and (
            not age or payload["scoringParameters"] != ["preferredAge-older_adult"]
        ):
            raise ValueError("Unsupported scoring parameters")
        filters = [parse_filter(payload["filter"])] if "filter" in payload else []
        vectors = payload.get("vectorQueries", [])
        if not isinstance(vectors, list) or len(vectors) > 1:
            raise ValueError("Expected at most one vector query")
        vector = None
        k = 50
        if vectors:
            item = vectors[0]
            if (
                not isinstance(item, dict)
                or item.keys() - {"kind", "vector", "fields", "k"}
                or item.get("kind", "vector") != "vector"
                or item.get("fields", "content_vector") != "content_vector"
                or match_all
            ):
                raise ValueError("Unsupported vector query")
            vector = validate_vector(item.get("vector"))
            k = _integer(item.get("k", 50), 1000)
        semantic = query_type == "semantic" and not match_all
        if semantic and not self.reranker_endpoint:
            raise ValueError("Semantic ranking requires a reranker endpoint")
        size = max(top, 50 if semantic or vector is not None or age else top)
        base: dict[str, Any] = {
            "size": size,
            "_source": {"excludes": ["content_vector"]},
        }
        lexical: dict[str, Any] = (
            {"match_all": {}}
            if match_all
            else {
                "multi_match": {
                    "query": text,
                    "fields": fields,
                    "type": "best_fields",
                    "operator": "and" if payload.get("searchMode") == "all" else "or",
                }
            }
        )
        lexical = {"bool": {"must": [lexical], "filter": filters}}
        if age and not match_all:
            lexical = {
                "function_score": {
                    "query": lexical,
                    "functions": [
                        {"filter": {"term": {"age_group": "older_adult"}}, "weight": 1.1}
                    ],
                    "score_mode": "multiply",
                    "boost_mode": "multiply",
                }
            }
        body = {**base, "query": lexical}
        if ordered:
            body["sort"] = [{"chunk_index": "asc"}, {"id": "asc"}]
            body["size"] = top
        url = f"{self.endpoint}/{index}/_search"
        lexical_hits = _hits(response_json(await request(self.client, "POST", url, json=body)))
        lists = [lexical_hits]
        if vector is not None:
            knn: dict[str, Any] = {"vector": vector, "k": k}
            if filters:
                knn["filter"] = {"bool": {"filter": filters}}
            vector_body = {
                **base,
                "size": k,
                "query": {"knn": {"content_vector": knn}},
            }
            lists.append(
                _hits(response_json(await request(self.client, "POST", url, json=vector_body)))
            )
        sources: dict[str, dict[str, Any]] = {}
        scores: dict[str, float] = {}
        for hits in lists:
            for rank, (identity, source, score) in enumerate(hits, 1):
                sources.setdefault(identity, source)
                scores[identity] = scores.get(identity, 0.0) + (
                    1 / (60 + rank) if vector is not None else score
                )
        if age and vector is not None:
            for identity, source in sources.items():
                if source.get("age_group") == "older_adult":
                    scores[identity] *= 1.1
        identities = (
            list(sources) if ordered else sorted(sources, key=lambda identity: -scores[identity])
        )
        reranker_scores: dict[str, float] = {}
        if semantic and identities:
            texts = []
            for identity in identities:
                source = sources[identity]
                if not all(isinstance(source.get(field), str) for field in ("title", "content")):
                    fail("reranker candidate lacks text")
                texts.append(source["title"] + "\n" + source["content"])
            for start in range(0, len(identities), 64):
                batch_ids = identities[start : start + 64]
                data = response_json(
                    await request(
                        self.client,
                        "POST",
                        f"{self.reranker_endpoint}/rerank",
                        json={
                            "query": text,
                            "texts": texts[start : start + 64],
                            "truncate": False,
                            "raw_scores": False,
                            "return_text": False,
                        },
                    )
                )
                if not isinstance(data, list) or len(data) != len(batch_ids):
                    fail("incomplete reranker response")
                seen = set()
                for item in data:
                    if (
                        not isinstance(item, dict)
                        or type(item.get("index")) is not int
                        or not 0 <= item["index"] < len(batch_ids)
                        or item["index"] in seen
                        or not _finite_number(item.get("score"))
                    ):
                        fail("malformed reranker response")
                    seen.add(item["index"])
                    reranker_scores[batch_ids[item["index"]]] = float(item["score"])
            identities.sort(key=lambda identity: -reranker_scores[identity])
        result = []
        for identity in identities[:top]:
            source = sources[identity]
            row = {
                field: value
                for field, value in source.items()
                if field in DOCUMENT_FIELDS and (selected is None or field in selected)
            }
            row["@search.score"] = scores[identity]
            if identity in reranker_scores:
                row["@search.rerankerScore"] = reranker_scores[identity]
            result.append(row)
        return result
