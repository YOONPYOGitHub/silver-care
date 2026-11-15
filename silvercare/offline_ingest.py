"""Publish prepared runtime JSONL into *new* private OpenSearch indexes.

Example::

    python -m silvercare.offline_ingest --endpoint http://opensearch:9200 \
      --embedding-endpoint http://embedding:80 --knowledge knowledge.jsonl \
      --cases cases.jsonl --knowledge-index local-knowledge-v1 \
      --case-index local-cases-v1 --output ingest-report.json

Only prepared SearchDocument records are accepted, never source archives or
evaluation cases. No index is deleted, updated, or reused. A failed publication
leaves its new indexes in place for inspection; retry with new names. Reports
contain hashes/counts, not document text. The built-in CJK analyzer is deliberate:
stock OpenSearch needs no Nori plugin and no failed-analyzer fallback is attempted.
"""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from silvercare.data import SearchDocument
from silvercare.expanded import holdout
from silvercare.offline_search import (
    DOCUMENT_FIELDS,
    endpoint_base,
    fail,
    index_name,
    request,
    response_json,
    validate_vector,
)
from silvercare.privacy import normalize, redact, redact_source

MAX_LINE_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 8 * 1024 * 1024
logger = logging.getLogger(__name__)


def index_schema(dimensions: int) -> dict[str, Any]:
    if type(dimensions) is not int or not 1 <= dimensions <= 16000:
        raise ValueError("Embedding dimensions must be between 1 and 16000")
    if set(SearchDocument.model_fields) != DOCUMENT_FIELDS:
        raise ValueError("SearchDocument schema changed; update the offline mappings")
    properties: dict[str, Any] = {field: {"type": "keyword"} for field in sorted(DOCUMENT_FIELDS)}
    for field in ("title", "content"):
        properties[field] = {"type": "text", "analyzer": "cjk", "similarity": "BM25"}
    properties["chunk_index"] = {"type": "integer"}
    properties["content_vector"] = {
        "type": "knn_vector",
        "dimension": dimensions,
        "method": {
            "name": "hnsw",
            "engine": "lucene",
            "space_type": "cosinesimil",
            "parameters": {"ef_construction": 128, "m": 16},
        },
    }
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {"dynamic": "strict", "properties": properties},
    }


def _documents(path: Path, role: str) -> Iterator[tuple[SearchDocument, bytes]]:
    if role not in {"knowledge", "cases"}:
        raise ValueError("Unknown corpus role")
    with path.open("rb") as stream:
        line_number = 0
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError(f"{role} line {line_number} exceeds the document size limit")
            if not raw.strip():
                continue
            try:
                document = SearchDocument.model_validate_json(raw, strict=True)
            except ValidationError:
                raise ValueError(f"Invalid {role} SearchDocument at line {line_number}") from None
            if (
                not document.id.strip()
                or len(document.id.encode()) > 512
                or not document.title.strip()
                or not document.content.strip()
                or not document.license.strip()
                or redact(document.title) != normalize(document.title)
                or redact(document.source_url) != normalize(document.source_url)
                or redact_source(document.content, document.dataset_id)
                != normalize(document.content)
            ):
                raise ValueError(f"Empty, oversized, or unredacted {role} record at {line_number}")
            provenance = urlsplit(document.source_url)
            if (
                provenance.scheme != "https"
                or not provenance.hostname
                or provenance.username is not None
                or provenance.password is not None
                or provenance.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError(f"Invalid public provenance in {role} record at {line_number}")
            if provenance.hostname.endswith((".local", ".internal")):
                raise ValueError(f"Private provenance in {role} record at {line_number}")
            try:
                address = ipaddress.ip_address(provenance.hostname)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError(f"Private provenance in {role} record at {line_number}")
            if document.split_origin.lower() not in {"", "training", "validation"}:
                raise ValueError(f"Holdout record in {role} at line {line_number}")
            if role == "knowledge":
                if document.evidence_kind not in {"reference", "dataset_knowledge"}:
                    raise ValueError(f"Case record in knowledge corpus at line {line_number}")
                if not document.parent_id.strip():
                    raise ValueError(f"Missing contextual parent in knowledge at {line_number}")
            else:
                if document.evidence_kind not in {"dialogue", "qa_case"}:
                    raise ValueError(f"Knowledge record in case corpus at line {line_number}")
                if document.evidence_kind == "qa_case":
                    if (
                        not document.content.startswith("질문·선택지:\n")
                        or "\n라벨 정답:\n" not in document.content
                    ):
                        raise ValueError(f"Unrecognized prepared QA case at line {line_number}")
                    question = document.content.removeprefix("질문·선택지:\n").split(
                        "\n라벨 정답:\n", 1
                    )[0]
                else:
                    question = document.content
                if holdout(question):
                    raise ValueError(f"Stable evaluation holdout in cases at line {line_number}")
            yield document, raw


def inspect_corpus(path: Path, role: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    count = 0
    seen = set()
    kinds: dict[str, int] = {}
    for document, _ in _documents(path, role):
        if document.id in seen:
            raise ValueError(f"Duplicate document identity in {role}")
        seen.add(document.id)
        count += 1
        kinds[document.evidence_kind] = kinds.get(document.evidence_kind, 0) + 1
    if not count:
        raise ValueError(f"Empty {role} corpus")
    return {"source_sha256": digest.hexdigest(), "documents": count, "evidence_kinds": kinds}


def _batches(path: Path, role: str, size: int) -> Iterator[list[SearchDocument]]:
    batch: list[SearchDocument] = []
    byte_count = 0
    for document, raw in _documents(path, role):
        if batch and (len(batch) == size or byte_count + len(raw) > MAX_BATCH_BYTES):
            yield batch
            batch, byte_count = [], 0
        batch.append(document)
        byte_count += len(raw)
    if batch:
        yield batch


async def _absent(client: httpx.AsyncClient, endpoint: str, index: str) -> None:
    try:
        response = await client.head(f"{endpoint}/{index}")
    except httpx.RequestError as exc:
        logger.warning("Offline ingest index preflight transport error: %s", type(exc).__name__)
        fail("index existence preflight failed")
    if response.status_code == 404:
        return
    if response.is_success:
        raise ValueError("Refusing to overwrite an existing index")
    fail(f"index existence preflight HTTP {response.status_code}", response.status_code)


def _acknowledged(data: Any, *, creation: bool = False) -> None:
    if not isinstance(data, dict) or data.get("acknowledged") is not True:
        fail("index creation was not acknowledged")
    if creation and data.get("shards_acknowledged") is not True:
        fail("index shards are not ready")


async def _upload(
    client: httpx.AsyncClient,
    endpoint: str,
    embedding_endpoint: str,
    index: str,
    path: Path,
    role: str,
    dimensions: int,
    batch_size: int,
) -> int:
    uploaded = 0
    for batch in _batches(path, role, batch_size):
        texts = [document.title + "\n" + document.content for document in batch]
        data = response_json(
            await request(
                client,
                "POST",
                f"{embedding_endpoint}/embed",
                json={"inputs": texts, "truncate": False},
            )
        )
        if not isinstance(data, list) or len(data) != len(batch):
            fail("incomplete embedding batch")
        try:
            vectors = [validate_vector(item, dimensions) for item in data]
        except ValueError:
            fail("invalid embedding batch")
        lines = []
        for document, vector in zip(batch, vectors, strict=True):
            lines.append(json.dumps({"create": {"_index": index, "_id": document.id}}))
            lines.append(
                json.dumps(
                    {**document.model_dump(), "content_vector": vector},
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        result = response_json(
            await request(
                client,
                "POST",
                f"{endpoint}/_bulk",
                content=("\n".join(lines) + "\n").encode(),
                headers={"Content-Type": "application/x-ndjson"},
            )
        )
        if (
            not isinstance(result, dict)
            or result.get("errors") is not False
            or not isinstance(result.get("items"), list)
            or len(result["items"]) != len(batch)
        ):
            fail("failed or incomplete bulk publication")
        for document, item in zip(batch, result["items"], strict=True):
            if not isinstance(item, dict) or set(item) != {"create"}:
                fail("malformed bulk item")
            operation = item["create"]
            if (
                not isinstance(operation, dict)
                or type(operation.get("status")) is not int
                or operation.get("status") != 201
                or operation.get("_id") != document.id
                or operation.get("_index") != index
                or "error" in operation
            ):
                fail("bulk document was not created")
        uploaded += len(batch)
    return uploaded


def _shards(data: Any) -> None:
    if not isinstance(data, dict):
        fail("malformed readiness response")
    shards = data.get("_shards")
    if (
        not isinstance(shards, dict)
        or type(shards.get("failed")) is not int
        or shards["failed"] != 0
        or type(shards.get("successful")) is not int
        or shards["successful"] < 1
        or type(shards.get("total")) is not int
        or shards.get("total") != shards["successful"]
    ):
        fail("index readiness shard failure")


async def ingest(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    embedding_endpoint: str,
    knowledge: Path,
    cases: Path,
    knowledge_index: str,
    case_index: str,
    dimensions: int = 1024,
    batch_size: int = 32,
    embedding_model: str = "",
    embedding_revision: str = "",
) -> dict[str, Any]:
    """Validate both corpora and targets before creating either new index."""
    endpoint, embedding_endpoint = endpoint_base(endpoint), endpoint_base(embedding_endpoint)
    index_name(knowledge_index)
    index_name(case_index)
    knowledge_path = await asyncio.to_thread(knowledge.resolve)
    cases_path = await asyncio.to_thread(cases.resolve)
    if knowledge_index == case_index or knowledge_path == cases_path:
        raise ValueError("Knowledge and case corpora/indexes must remain separate")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise ValueError("Batch size must be between 1 and 128")
    schema = index_schema(dimensions)
    corpora = [
        ("knowledge", knowledge, knowledge_index),
        ("cases", cases, case_index),
    ]
    reports = {
        role: await asyncio.to_thread(inspect_corpus, path, role) for role, path, _ in corpora
    }
    for _, _, index in corpora:
        await _absent(client, endpoint, index)
    for _, _, index in corpora:
        data = response_json(await request(client, "PUT", f"{endpoint}/{index}", json=schema))
        _acknowledged(data, creation=True)
        if data.get("index") != index:
            fail("created index identity does not match the explicit target")
    for role, path, index in corpora:
        uploaded = await _upload(
            client, endpoint, embedding_endpoint, index, path, role, dimensions, batch_size
        )
        refreshed = response_json(await request(client, "POST", f"{endpoint}/{index}/_refresh"))
        _shards(refreshed)
        counted = response_json(await request(client, "GET", f"{endpoint}/{index}/_count"))
        _shards(counted)
        if (
            type(counted.get("count")) is not int
            or counted["count"] != reports[role]["documents"]
            or uploaded != reports[role]["documents"]
        ):
            fail("index document count does not match the corpus")
        if await asyncio.to_thread(inspect_corpus, path, role) != reports[role]:
            raise ValueError("Source corpus changed during publication")
        reports[role].update(
            {"index": index, "uploaded": uploaded, "verified_count": counted["count"]}
        )
    return {
        "format_version": 1,
        "analyzer": "cjk",
        "analyzer_policy": "explicit stock built-in CJK; no Nori fallback",
        "retrieval": {"text": "BM25", "vector": "lucene/hnsw/cosinesimil", "fusion": "RRF/60"},
        "embedding": {
            "dimensions": dimensions,
            "model": embedding_model or None,
            "revision": embedding_revision or None,
            "model_metadata_source": "operator-provided" if embedding_model else "not provided",
            "truncate": False,
        },
        "corpora": reports,
        "publication_mode": "new_indexes_only",
        "holdouts": "rejected using the prepared-case stable SHA256 split",
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--embedding-endpoint", required=True)
    parser.add_argument("--knowledge", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--knowledge-index", required=True)
    parser.add_argument("--case-index", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dimensions", default=1024, type=int)
    parser.add_argument("--batch-size", default=32, type=int)
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-revision", default="")
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> None:
    if args.output.resolve() in {args.knowledge.resolve(), args.cases.resolve()}:
        raise ValueError("Report must not overwrite an input corpus")
    if args.output.exists() or not args.output.parent.is_dir():
        raise ValueError("Report requires a new file in an existing directory")
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
        report = await ingest(
            client,
            endpoint=args.endpoint,
            embedding_endpoint=args.embedding_endpoint,
            knowledge=args.knowledge,
            cases=args.cases,
            knowledge_index=args.knowledge_index,
            case_index=args.case_index,
            dimensions=args.dimensions,
            batch_size=args.batch_size,
            embedding_model=args.embedding_model,
            embedding_revision=args.embedding_revision,
        )
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"report": str(args.output), "status": "verified"}))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main(_arguments()))
