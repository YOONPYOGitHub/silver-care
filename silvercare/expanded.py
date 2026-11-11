import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Literal

from silvercare.archives import source_files
from silvercare.azure import SEARCH_API, SYSTEM_PROMPT, AzureServices, UpstreamError
from silvercare.config import Settings, get_settings
from silvercare.corpus import CorpusCatalog
from silvercare.data import SearchDocument, care_audience, index_schema, prepare
from silvercare.privacy import normalize, redact

OUTPUT = Path("data/processed/expanded")
KNOWLEDGE_INDEX = "silvercare-knowledge-v4"
CASE_INDEX = "silvercare-cases-v4"
# AI Hub 86 data guide, page 10; source and document hash are recorded in the RAG guide.
AGE_GROUPS = {
    "A01": "adolescent",
    "A02": "young_adult",
    "A03": "middle_adult",
    "A04": "older_adult",
}


def holdout(text: str) -> bool:
    canonical = re.sub(r"\s+", "", normalize(text))
    return int(hashlib.sha256(canonical.encode()).hexdigest()[:8], 16) % 5 == 0


def load_documents(path: Path) -> list[SearchDocument]:
    return [
        SearchDocument.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def case_documents(
    catalog: CorpusCatalog,
) -> tuple[list[SearchDocument], list[SearchDocument], dict[str, int]]:
    runtime: list[SearchDocument] = []
    evaluation: list[SearchDocument] = []
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for source in catalog.sources:
        if source.purpose not in {"qa", "dialogue"}:
            continue
        for name, raw in source_files(source.raw_path):
            if not name.lower().endswith(".json"):
                continue
            data = json.loads(raw.decode("utf-8-sig"))
            records = data if isinstance(data, list) else [data]
            for record in records:
                counts["records"] += 1
                kind: Literal["qa_case", "dialogue"]
                age_code = ""
                if "question" in record:
                    question = normalize(record["question"])
                    answer = normalize(record["answer"])
                    key = f"qa:{question}"
                    content = f"질문·선택지:\n{question}\n라벨 정답:\n{answer}"
                    qa_type = str(record["q_type"])
                    negative = bool(re.search(r"옳지|아닌|틀린|잘못|적절하지", question))
                    interpretation = (
                        "incorrect_choice"
                        if qa_type == "1" and negative
                        else "correct_choice"
                        if qa_type == "1"
                        else "unverified_answer"
                    )
                    record_id = str(record["qa_id"])
                    kind = "qa_case"
                    title = "질문 사례 · " + question[:90]
                    split = "validation" if "validation" in source.name else "training"
                elif "talk" in record:
                    talk = record["talk"]
                    turns = talk["content"]
                    parts = [
                        f"{'사용자' if key.startswith('HS') else '응답 예시'}: {value}"
                        for key, value in turns.items()
                        if isinstance(value, str) and value.strip()
                    ]
                    content = normalize("\n".join(parts))
                    first = str(turns.get("HS01", ""))
                    key = f"dialogue:{content}"
                    question = content
                    record_id = str(talk["id"]["talk-id"])
                    kind, qa_type, interpretation = "dialogue", "", "dialogue_example"
                    title = "대화 사례 · " + normalize(first)[:90]
                    split = "validation"
                    age_codes = [
                        value
                        for value in record.get("profile", {}).get("persona", {}).get("human", [])
                        if isinstance(value, str) and re.fullmatch(r"A\d{2}", value)
                    ]
                    if len(age_codes) > 1:
                        raise ValueError(f"{source.name}: conflicting age codes")
                    age_code = age_codes[0] if age_codes else ""
                    if age_code and source.dataset_id == "86" and age_code not in AGE_GROUPS:
                        raise ValueError(f"{source.name}: unknown age code {age_code}")
                else:
                    raise ValueError(f"{source.name}: unrecognized case schema")
                digest = hashlib.sha256(key.encode()).hexdigest()
                if digest in seen:
                    counts["duplicates"] += 1
                    continue
                seen.add(digest)
                if redact(content) != content or len(content) < 20:
                    counts["sensitive_or_empty"] += 1
                    continue
                document = SearchDocument(
                    id=hashlib.sha256(
                        f"{source.dataset_id}:{source.file_key}:{kind}:{record_id}".encode()
                    ).hexdigest(),
                    title=title,
                    content=content,
                    source_url=(
                        "https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=" + source.dataset_id
                    ),
                    dataset_id=f"aihub-{source.dataset_id}-cases",
                    evidence_kind=kind,
                    license="AI Hub dataset terms; NIA project attribution",
                    updated_at=date.today().isoformat(),
                    source_record_id=record_id,
                    source_file_key=source.file_key,
                    source_display="공개 대화 자료" if kind == "dialogue" else "공개 의학 QA 자료",
                    review_status="automated",
                    risk_level="clinical" if kind == "qa_case" else "general",
                    split_origin=split,
                    qa_type=qa_type,
                    answer_interpretation=interpretation,
                    age_code=age_code,
                    age_group=AGE_GROUPS.get(age_code, "unknown")
                    if source.dataset_id == "86"
                    else "unknown",
                )
                if holdout(question):
                    evaluation.append(document)
                    counts["holdout"] += 1
                else:
                    runtime.append(document)
                    counts[kind] += 1
    return runtime, evaluation, dict(counts)


def write_documents(path: Path, documents: list[SearchDocument]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".partial")
    try:
        with temporary.open("w") as stream:
            temporary.chmod(0o600)
            for document in documents:
                stream.write(document.model_dump_json() + "\n")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def prepare_expanded(external_path: Path | None) -> dict[str, Any]:
    catalog = CorpusCatalog.model_validate_json(Path("data/catalog.json").read_text())
    knowledge: list[SearchDocument] = []
    for source in catalog.sources:
        if source.purpose != "knowledge":
            continue
        manifest = source.manifest()
        assert source.prepared_path is not None
        prepare(manifest, source.raw_path, source.prepared_path)
        knowledge.extend(load_documents(source.prepared_path))
    knowledge.extend(load_documents(Path("data/processed/reference.jsonl")))
    if external_path is not None:
        knowledge.extend(load_documents(external_path))
    for document in knowledge:
        if document.dataset_id == "mohw-care-2026" and document.audience == "general":
            document.audience = care_audience(document.title)
    seen: set[str] = set()
    selected = []
    for document in knowledge:
        digest = hashlib.sha256(normalize(document.content).encode()).hexdigest()
        if digest not in seen:
            selected.append(document)
            seen.add(digest)
    cases, evaluation, counts = case_documents(catalog)
    write_documents(OUTPUT / "knowledge.jsonl", selected)
    write_documents(OUTPUT / "cases.jsonl", cases)
    write_documents(OUTPUT / "evaluation.jsonl", evaluation)
    report = {
        "knowledge_documents": len(selected),
        "case_documents": len(cases),
        "evaluation_documents": len(evaluation),
        "case_processing": counts,
        "knowledge_by_dataset": dict(Counter(d.dataset_id for d in selected)),
        "risk_levels": dict(Counter(d.risk_level for d in selected)),
        "knowledge_by_audience": dict(Counter(d.audience for d in selected)),
        "dialogue_by_age_code": dict(
            Counter(d.age_code for d in cases if d.evidence_kind == "dialogue")
        ),
        "dialogue_by_age_group": dict(
            Counter(d.age_group for d in cases if d.evidence_kind == "dialogue")
        ),
        "holdout_rule": "Stable normalized question/conversation SHA256, 20%; never indexed",
        "qa_rule": "Full question/options/label preserved, not verified factual evidence",
    }
    report_path = OUTPUT / "inventory.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report_path.chmod(0o600)
    return report


class EmbeddingCache:
    def __init__(self, path: Path, model: str, dimensions: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
        )
        self.prefix = f"{model}:{dimensions}:"

    def key(self, text: str) -> str:
        return hashlib.sha256((self.prefix + text).encode()).hexdigest()

    def get(self, text: str) -> list[float] | None:
        row = self.connection.execute(
            "SELECT vector FROM embeddings WHERE key=?", (self.key(text),)
        ).fetchone()
        if row is None:
            return None
        values: list[float] = json.loads(row[0])
        return values

    def put(self, text: str, vector: list[float]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO embeddings(key, vector) VALUES (?,?)",
            (self.key(text), json.dumps(vector)),
        )

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()


def validate_publish_target(
    settings: Settings, index: str, documents: list[SearchDocument]
) -> None:
    if index not in {KNOWLEDGE_INDEX, CASE_INDEX}:
        raise ValueError("Only explicitly configured staging indexes can be reconciled")
    protected = {settings.azure_search_index, settings.azure_search_case_index}
    release_path = OUTPUT / "release.json"
    if release_path.exists():
        release_settings = json.loads(release_path.read_text())
        fields = ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX", "AZURE_SEARCH_CASE_INDEX")
        if not isinstance(release_settings, dict) or not all(
            isinstance(release_settings.get(field), str) and release_settings[field]
            for field in fields
        ):
            raise ValueError("Cannot verify active indexes from the recorded release")
        if release_settings["AZURE_SEARCH_ENDPOINT"].rstrip("/") == (
            settings.azure_search_endpoint.rstrip("/")
        ):
            protected.update(release_settings[field] for field in fields[1:])
    if index in protected:
        raise ValueError("Do not overwrite a configured or recorded live index")
    if not documents:
        raise ValueError("An expanded index must not be empty")
    if len({document.id for document in documents}) != len(documents):
        raise ValueError("Duplicate document IDs")
    allowed = (
        {"reference", "dataset_knowledge"} if index == KNOWLEDGE_INDEX else {"dialogue", "qa_case"}
    )
    if any(document.evidence_kind not in allowed for document in documents):
        raise ValueError("Prepared evidence kinds do not match the staging index")


async def upload(
    services: AzureServices, cache: EmbeddingCache, index: str, path: Path
) -> dict[str, int | str]:
    raw = await asyncio.to_thread(path.read_bytes)
    documents = [
        SearchDocument.model_validate_json(line) for line in raw.splitlines() if line.strip()
    ]
    await asyncio.to_thread(validate_publish_target, services.settings, index, documents)
    base = services.settings.azure_search_endpoint.rstrip("/")
    headers = {"Authorization": f"Bearer {await services.token(search=True)}"}
    await services.request(
        "search",
        "PUT",
        f"{base}/indexes/{index}?api-version={SEARCH_API}",
        headers=headers,
        json=index_schema(index, services.settings.embedding_dimensions),
    )
    existing: dict[str, str] = {}
    skip = 0
    fields = list(SearchDocument.model_fields)
    while True:
        response = await services.request(
            "search",
            "POST",
            f"{base}/indexes/{index}/docs/search?api-version={SEARCH_API}",
            headers=headers,
            json={"search": "*", "select": ",".join(fields), "top": 1000, "skip": skip},
        )
        rows = response.json()["value"]
        for row in rows:
            serialized = json.dumps({key: row.get(key) for key in fields}, sort_keys=True)
            existing[row["id"]] = hashlib.sha256(serialized.encode()).hexdigest()
        if len(rows) < 1000:
            break
        skip += 1000
    changed = [
        doc
        for doc in documents
        if existing.get(doc.id)
        != hashlib.sha256(json.dumps(doc.model_dump(), sort_keys=True).encode()).hexdigest()
    ]
    generated = 0
    for start in range(0, len(changed), 128):
        batch = changed[start : start + 128]
        texts = [document.title + "\n" + document.content for document in batch]
        vectors = [cache.get(text) for text in texts]
        missing = [i for i, vector in enumerate(vectors) if vector is None]
        for offset in range(0, len(missing), 32):
            indices = missing[offset : offset + 32]
            embedded = await services.embed([texts[i] for i in indices])
            for i, vector in zip(indices, embedded, strict=True):
                vectors[i] = vector
                cache.put(texts[i], vector)
            cache.connection.commit()
        generated += len(missing)
        payload = [
            {
                "@search.action": "mergeOrUpload",
                **document.model_dump(),
                "content_vector": vectors[i],
            }
            for i, document in enumerate(batch)
        ]
        result = await services.request(
            "search",
            "POST",
            f"{base}/indexes/{index}/docs/index?api-version={SEARCH_API}",
            headers=headers,
            json={"value": payload},
        )
        if any(not row["status"] for row in result.json()["value"]):
            raise ValueError("Expanded index contains failed document uploads; no release allowed")
        if start % 320 == 0:
            print(
                json.dumps(
                    {
                        "index": index,
                        "uploaded": min(start + 128, len(changed)),
                        "total": len(changed),
                    }
                ),
                flush=True,
            )
    # Reconcile only the explicitly owned staging indexes, never the existing pilot.
    stale = sorted(set(existing) - {document.id for document in documents})
    for start in range(0, len(stale), 100):
        result = await services.request(
            "search",
            "POST",
            f"{base}/indexes/{index}/docs/index?api-version={SEARCH_API}",
            headers=headers,
            json={
                "value": [
                    {"@search.action": "delete", "id": key} for key in stale[start : start + 100]
                ]
            },
        )
        if any(not row["status"] for row in result.json()["value"]):
            raise ValueError("Stale document deletion failed")
    return {
        "documents": len(documents),
        "new_embeddings": generated,
        "uploaded_changes": len(changed),
        "unchanged": len(documents) - len(changed),
        "deleted_stale": len(stale),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


async def publish() -> None:
    services = AzureServices(get_settings())
    cache: EmbeddingCache | None = None
    targets = [(KNOWLEDGE_INDEX, "knowledge.jsonl"), (CASE_INDEX, "cases.jsonl")]
    report: dict[str, Any] = {
        "endpoint": services.settings.azure_search_endpoint.rstrip("/"),
        "embedding_deployment": services.settings.azure_embedding_deployment,
        "dimensions": services.settings.embedding_dimensions,
        "indexes": {},
    }
    try:
        if not services.settings.azure_search_endpoint or not (
            services.settings.azure_embedding_deployment
        ):
            raise ValueError("Publishing requires a search endpoint and embedding deployment")
        for index, name in targets:
            documents = await asyncio.to_thread(load_documents, OUTPUT / name)
            await asyncio.to_thread(validate_publish_target, services.settings, index, documents)
        cache = EmbeddingCache(
            OUTPUT / "embeddings.sqlite3",
            services.settings.azure_embedding_deployment,
            services.settings.embedding_dimensions,
        )
        for index, name in targets:
            result = await upload(services, cache, index, OUTPUT / name)
            report["indexes"][index] = {
                **result,
                "file": name,
                "sha256": result["source_sha256"],
            }
            print(json.dumps({"index": index, **result}), flush=True)
        path = OUTPUT / "publish-report.json"
        path.write_text(json.dumps(report, indent=2))
        path.chmod(0o600)
    finally:
        if cache is not None:
            cache.close()
        await services.close()


async def release() -> None:
    report = json.loads((OUTPUT / "publish-report.json").read_text())
    quality = json.loads((OUTPUT / "retrieval-evaluation-final.json").read_text())
    audience = json.loads((OUTPUT / "audience-evaluation.json").read_text())
    services = AzureServices(get_settings())
    try:
        if report.get("endpoint") != services.settings.azure_search_endpoint.rstrip("/"):
            raise ValueError("Published search endpoint does not match release configuration")
        if (
            report["embedding_deployment"] != services.settings.azure_embedding_deployment
            or report["dimensions"] != services.settings.embedding_dimensions
        ):
            raise ValueError(
                "Published embedding configuration does not match release configuration"
            )
        if (
            not quality.get("nonregression_passed")
            or quality.get("index") != KNOWLEDGE_INDEX
            or quality.get("endpoint") != services.settings.azure_search_endpoint
            or quality.get("knowledge_sha256") != report["indexes"][KNOWLEDGE_INDEX]["sha256"]
            or quality["hybrid_pipeline"]["rewrite_queries"] != services.settings.rewrite_queries
            or quality["hybrid_pipeline"]["semantic_ranking"] != services.settings.semantic_ranking
        ):
            raise ValueError("Verified retrieval evaluation does not match this release")
        if (
            not audience.get("passed")
            or audience.get("prompt_sha256") != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
            or audience.get("scenarios_sha256")
            != hashlib.sha256(
                await asyncio.to_thread(Path("data/audience-evaluation.json").read_bytes)
            ).hexdigest()
            or audience.get("knowledge_sha256") != report["indexes"][KNOWLEDGE_INDEX]["sha256"]
            or audience.get("cases_sha256") != report["indexes"][CASE_INDEX]["sha256"]
            or audience.get("endpoint") != services.settings.azure_search_endpoint
            or audience.get("index") != KNOWLEDGE_INDEX
            or audience.get("case_index") != CASE_INDEX
            or audience.get("official_health_enabled")
            != bool(services.settings.kdca_health_api_key.get_secret_value())
        ):
            raise ValueError("Verified audience response evaluation does not match this release")
        base = services.settings.azure_search_endpoint
        headers = {"Authorization": f"Bearer {await services.token(search=True)}"}
        for index, item in report["indexes"].items():
            path = OUTPUT / item["file"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Prepared corpus changed after publishing")
            for attempt in range(10):
                result = await services.request(
                    "search",
                    "POST",
                    f"{base}/indexes/{index}/docs/search?api-version={SEARCH_API}",
                    headers=headers,
                    json={"search": "*", "top": 0, "count": True},
                )
                if result.json()["@odata.count"] == item["documents"]:
                    break
                if attempt == 9:
                    raise ValueError("Published document count has not converged; refusing release")
                await asyncio.sleep(2)
        stats = await services.request(
            "search",
            "GET",
            f"{base}/servicestats?api-version={SEARCH_API}",
            headers=headers,
        )
        storage = stats.json()["counters"]["storageSize"]
        if storage["quota"] and storage["usage"] >= storage["quota"]:
            raise ValueError("Search storage capacity exhausted; refusing release")
        settings = {
            "AZURE_SEARCH_ENDPOINT": base,
            "AZURE_SEARCH_INDEX": KNOWLEDGE_INDEX,
            "AZURE_SEARCH_CASE_INDEX": CASE_INDEX,
            "RETRIEVAL_MODE": "hybrid",
            "AZURE_EMBEDDING_DEPLOYMENT": services.settings.azure_embedding_deployment,
            "EMBEDDING_DIMENSIONS": str(services.settings.embedding_dimensions),
            "REWRITE_QUERIES": str(services.settings.rewrite_queries).lower(),
            "SEMANTIC_RANKING": str(services.settings.semantic_ranking).lower(),
        }
        path = OUTPUT / "release.json"
        path.write_text(json.dumps(settings, indent=2))
        path.chmod(0o600)
        print(json.dumps({"release": settings, "storage": storage}))
    finally:
        await services.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and publish the expanded hybrid RAG corpus")
    parser.add_argument("command", choices=["prepare", "publish", "release"])
    parser.add_argument("--external", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(json.dumps(prepare_expanded(args.external), ensure_ascii=False, indent=2))
        elif args.command == "publish":
            asyncio.run(publish())
        else:
            asyncio.run(release())
    except (ValueError, OSError, sqlite3.Error, UpstreamError) as exc:
        parser.exit(1, f"Expanded corpus operation failed: {exc}\n")


if __name__ == "__main__":
    main()
