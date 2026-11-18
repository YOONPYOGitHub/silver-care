import argparse
import asyncio
import csv
import hashlib
import json
import logging
import re
import tarfile
import zipfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from silvercare.archives import MAX_MEMBER, source_files
from silvercare.azure import SEARCH_API, AzureServices
from silvercare.config import DataSourceSettings, get_settings
from silvercare.privacy import normalize, redact
from silvercare.retrieval import Domain, Jurisdiction, Language, SourceType

logger = logging.getLogger(__name__)
AIHUB_BASE = "https://api.aihub.or.kr"
EvidenceKind = Literal["reference", "dialogue", "dataset_knowledge", "qa_case"]


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    dataset_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_url: str = ""
    evidence_kind: EvidenceKind
    license: str = Field(min_length=1)
    license_reviewed: bool = False
    human_reviewed: bool = False
    review_method: Literal["none", "automated", "human"] = "none"
    source_schema: Literal["generic", "medical_source"] = "generic"
    source_file_key: str = ""
    split: Literal["training", "validation", "test"] = "training"
    selected_record_ids: list[str] = Field(default_factory=list)
    updated_at: date
    text_paths: list[str] = Field(default_factory=list)
    documents: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_rules(self) -> "DatasetManifest":
        host = urlparse(self.source_url).hostname or ""
        if host in {"aihub.or.kr", "www.aihub.or.kr"} and self.evidence_kind == "reference":
            raise ValueError("AI Hub conversation data must not be medical/factual evidence")
        if self.evidence_kind == "dataset_knowledge":
            if self.source_schema != "medical_source" or not self.source_file_key.isdigit():
                raise ValueError("Dataset knowledge requires a medical-source schema and file key")
        urls = [self.source_url] if self.source_url else []
        urls.extend(doc.get("source_url", "") for doc in self.documents)
        for value in urls:
            url = urlparse(value)
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError("Every source must have a valid public HTTPS provenance URL")
        return self


class SearchDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    content: str
    source_url: str
    dataset_id: str
    evidence_kind: EvidenceKind
    license: str
    updated_at: str
    source_record_id: str = ""
    source_file_key: str = ""
    source_display: str = ""
    source_year: str = ""
    review_status: str = ""
    topic: str = ""
    risk_level: str = "general"
    split_origin: str = ""
    qa_type: str = ""
    answer_interpretation: str = ""
    audience: Literal["general", "self", "caregiver", "provider"] = "general"
    age_code: str = ""
    age_group: str = "unknown"
    domain: Domain = "general"
    source_type: SourceType = "reference"
    source_language: Language = "unknown"
    jurisdiction: Jurisdiction = "unspecified"
    parent_id: str = ""
    chunk_index: int = Field(default=0, ge=0)
    drug_entities: list[str] = Field(default_factory=list)


class MedicalSource(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    c_id: str = Field(min_length=1)
    domain: int
    source: int
    source_spec: str = Field(min_length=1)
    creation_year: str | None
    content: str = Field(min_length=1)


CARE_TOPICS = {
    "영양·식사": r"영양|식사|식습관|(?<![가-힣])식이",
    "운동·신체활동": r"신체활동|신체 활동|규칙적인 운동|유산소 운동|근력|걷기|좌식",
    "낙상·안전": r"낙상|넘어지",
    "수면·휴식": r"수면|숙면",
    "인지·가족돌봄": r"인지기능|인지장애|치매|보호자",
    "노년기 생활": r"노인|고령|요양",
}
CLINICAL_CONTEXT = re.compile(
    r"약물|복약|복용|용량|投与|투여|처방|수술|시술|진단|검사|혈당|인슐린|MRI|CT"
)


def care_audience(title: str) -> Literal["general", "self", "caregiver", "provider"]:
    if re.search(r"사업관리|예산편성|집행기준|수행기관|전담사회복지사|생활지원사|종사자", title):
        return "provider"
    if re.search(r"가족.*지원|보호자.*안내|가족.*돌봄", title):
        return "caregiver"
    if re.search(r"신청방법|신청 방법|이용자.*안내|이용 안내", title):
        return "self"
    return "general"


def medical_records(path: Path) -> Iterator[MedicalSource]:
    for name, data in source_files(path):
        if not name.lower().endswith(".json"):
            continue
        value = json.loads(data.decode("utf-8-sig"))
        records = value if isinstance(value, list) else [value]
        for record in records:
            yield MedicalSource.model_validate(record)


def medical_screen(record: MedicalSource) -> tuple[str | None, str]:
    text = normalize(record.content)
    topic = next((name for name, pattern in CARE_TOPICS.items() if re.search(pattern, text)), None)
    if topic is None:
        topic = "의학·건강 정보"
    if redact(text) != text or redact(record.source_spec) != record.source_spec:
        return None, "sensitive_pattern"
    if len(text) < 40:
        return None, "incomplete_fragment"
    return topic, "selected"


def prepare_medical(manifest: DatasetManifest, source: Path, output: Path) -> dict[str, int]:
    if manifest.evidence_kind != "dataset_knowledge" or manifest.review_method == "none":
        raise ValueError("Medical-source preparation requires an explicit dataset review method")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_suffix(output.suffix + ".partial")
    counts: dict[str, int] = {"records": 0, "chunks": 0, "duplicates": 0}
    seen: set[str] = set()
    record_ids: set[str] = set()
    accepted_ids: set[str] = set()
    decisions: list[dict[str, str]] = []
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            for record in medical_records(source):
                counts["records"] += 1
                if record.c_id in record_ids:
                    raise ValueError("Duplicate medical source record ID")
                record_ids.add(record.c_id)
                topic, reason = medical_screen(record)
                if (
                    topic is not None
                    and manifest.selected_record_ids
                    and record.c_id not in manifest.selected_record_ids
                ):
                    topic, reason = None, "not_selected_for_pilot"
                decisions.append({"record_id": record.c_id, "reason": reason})
                if topic is None:
                    counts[reason] = counts.get(reason, 0) + 1
                    continue
                content = normalize(record.content)
                digest = hashlib.sha256(content.encode()).hexdigest()
                if digest in seen:
                    counts["duplicates"] += 1
                    continue
                seen.add(digest)
                accepted_ids.add(record.c_id)
                year = record.creation_year or ""
                if year and year != "null" and not re.fullmatch(r"\d{4}", year):
                    raise ValueError("Invalid source year")
                base_id = f"{manifest.dataset_id}:{manifest.source_file_key}:{record.c_id}"
                for number, content_chunk in enumerate(chunks(content, size=1600, overlap=180)):
                    document = SearchDocument(
                        id=hashlib.sha256(f"{base_id}:{number}".encode()).hexdigest(),
                        title=f"{topic} · {record.source_spec}",
                        content=content_chunk,
                        source_url=manifest.source_url,
                        dataset_id=manifest.dataset_id,
                        evidence_kind="dataset_knowledge",
                        license=manifest.license,
                        updated_at=manifest.updated_at.isoformat(),
                        source_record_id=record.c_id,
                        source_file_key=manifest.source_file_key,
                        source_display=record.source_spec,
                        source_year="" if year == "null" else year,
                        review_status=manifest.review_method,
                        topic=topic,
                        risk_level="clinical" if CLINICAL_CONTEXT.search(content) else "general",
                        split_origin=manifest.split,
                    )
                    handle.write(document.model_dump_json() + "\n")
                    counts["chunks"] += 1
        if set(manifest.selected_record_ids) - record_ids:
            raise ValueError("Selected record IDs are not present in the source archive")
        if set(manifest.selected_record_ids) - accepted_ids:
            raise ValueError("An explicitly selected record no longer passes screening")
        if not counts["chunks"]:
            raise ValueError("No medical-source records passed the documented screening rules")
        temporary.replace(output)
    except (ValueError, OSError, UnicodeError, zipfile.BadZipFile, tarfile.TarError):
        temporary.unlink(missing_ok=True)
        raise
    report = {
        "dataset_id": manifest.dataset_id,
        "source_file_key": manifest.source_file_key,
        "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "review_status": manifest.review_method,
        "counts": counts,
        "decisions": decisions,
        "notice": "Automatically screened test corpus; not clinician-reviewed medical guidance.",
    }
    report_path = output.with_suffix(".report.json")
    with report_path.open("w", encoding="utf-8") as handle:
        report_path.chmod(0o600)
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return counts


def select_values(value: Any, parts: list[str]) -> Iterator[str]:
    if not parts:
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from select_values(item, [])
        return
    head, *tail = parts
    if head == "*":
        if isinstance(value, list):
            for item in value:
                yield from select_values(item, tail)
        elif isinstance(value, dict):
            for item in value.values():
                yield from select_values(item, tail)
    elif isinstance(value, dict) and head in value:
        yield from select_values(value[head], tail)


def records_from_bytes(name: str, data: bytes, paths: list[str]) -> Iterator[str]:
    if len(data) > MAX_MEMBER:
        raise ValueError(f"File exceeds 16MB safety limit: {name}")
    suffix = Path(name).suffix.lower()
    if suffix not in {".json", ".jsonl", ".txt", ".csv"}:
        return
    text = data.decode("utf-8-sig")
    if suffix == ".txt":
        yield text
        return
    if not paths:
        raise ValueError(
            "JSON/JSONL/CSV input requires explicit text_paths; metadata is never dumped"
        )
    if suffix == ".json":
        obj = json.loads(text)
        objects = obj if isinstance(obj, list) else [obj]
    elif suffix == ".jsonl":
        objects = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        objects = list(csv.DictReader(text.splitlines()))
    for obj in objects:
        selected = [
            line for path in paths for line in select_values(obj, path.split(".")) if line.strip()
        ]
        if selected:
            yield "\n".join(selected)


def raw_records(path: Path, paths: list[str]) -> Iterator[str]:
    for name, data in source_files(path):
        yield from records_from_bytes(name, data, paths)


def chunks(text: str, size: int = 1000, overlap: int = 120) -> Iterator[str]:
    if size <= overlap or overlap < 0:
        raise ValueError("Chunk size must be greater than overlap")
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            split = max(
                text.rfind("\n", start + size // 2, end), text.rfind(". ", start + size // 2, end)
            )
            if split > start + overlap:
                end = split + 1
        part = text[start:end].strip()
        if len(part) >= 20:
            yield part
        if end >= len(text):
            break
        start = end - overlap


def prepare(manifest: DatasetManifest, source: Path | None, output: Path) -> dict[str, int]:
    if not manifest.license_reviewed:
        raise ValueError("Review dataset-specific usage rights and set license_reviewed=true first")
    if not manifest.documents and source is None:
        raise ValueError("Specify --input for datasets without inline reference documents")
    if not manifest.documents and not manifest.source_url:
        raise ValueError("Raw datasets require a source_url")
    if manifest.source_schema == "medical_source":
        if source is None:
            raise ValueError("Medical-source preparation requires --input")
        return prepare_medical(manifest, source, output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    seen: set[str] = set()
    counts = {"records": 0, "redacted_records": 0, "duplicates": 0, "chunks": 0}
    if manifest.documents:
        records: Iterator[tuple[str, str, str]] = (
            (doc["content"], doc["title"], doc["source_url"]) for doc in manifest.documents
        )
    else:
        assert source is not None
        records = (
            (text, manifest.title, manifest.source_url)
            for text in raw_records(source, manifest.text_paths)
        )
    temporary = output.with_suffix(output.suffix + ".partial")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            for raw, title, source_url in records:
                counts["records"] += 1
                cleaned = redact(raw)
                counts["redacted_records"] += cleaned != normalize(raw)
                for content in chunks(cleaned):
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    if digest in seen:
                        counts["duplicates"] += 1
                        continue
                    seen.add(digest)
                    key = hashlib.sha256(f"{manifest.dataset_id}:{digest}".encode()).hexdigest()
                    document = SearchDocument(
                        id=key,
                        title=redact(title),
                        content=content,
                        source_url=source_url,
                        dataset_id=manifest.dataset_id,
                        evidence_kind=manifest.evidence_kind,
                        license=manifest.license,
                        updated_at=manifest.updated_at.isoformat(),
                    )
                    handle.write(document.model_dump_json() + "\n")
                    counts["chunks"] += 1
        if not counts["chunks"]:
            raise ValueError("No usable text found; check actual schema and text_paths")
        temporary.replace(output)
    except (ValueError, KeyError, OSError, UnicodeError, zipfile.BadZipFile, tarfile.TarError):
        temporary.unlink(missing_ok=True)
        raise
    return counts


def index_schema(name: str, vector_dimensions: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "name": name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "analyzer": "ko.microsoft"},
            {
                "name": "content",
                "type": "Edm.String",
                "searchable": True,
                "analyzer": "ko.microsoft",
            },
            *[
                {"name": field, "type": "Edm.String", "filterable": True}
                for field in (
                    "source_url",
                    "dataset_id",
                    "evidence_kind",
                    "license",
                    "updated_at",
                    "source_record_id",
                    "source_file_key",
                    "source_display",
                    "source_year",
                    "review_status",
                    "topic",
                    "risk_level",
                    "split_origin",
                    "qa_type",
                    "answer_interpretation",
                    "audience",
                    "age_code",
                    "age_group",
                    "domain",
                    "source_type",
                    "source_language",
                    "jurisdiction",
                    "parent_id",
                )
            ],
            {"name": "chunk_index", "type": "Edm.Int32", "filterable": True, "sortable": True},
            {"name": "drug_entities", "type": "Collection(Edm.String)", "filterable": True},
        ],
    }
    if vector_dimensions is not None:
        schema["fields"].append(
            {
                "name": "content_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "stored": False,
                "dimensions": vector_dimensions,
                "vectorSearchProfile": "care-vector",
            }
        )
        schema["vectorSearch"] = {
            "algorithms": [
                {"name": "care-hnsw", "kind": "hnsw", "hnswParameters": {"metric": "cosine"}}
            ],
            "profiles": [{"name": "care-vector", "algorithm": "care-hnsw"}],
        }
        schema["semantic"] = {
            "configurations": [
                {
                    "name": "care-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "title"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                    },
                }
            ]
        }
    schema["scoringProfiles"] = [
        {
            "name": "care-age",
            "functions": [
                {
                    "type": "tag",
                    "boost": 1.5,
                    "fieldName": "age_group",
                    "tag": {"tagsParameter": "preferredAge"},
                }
            ],
        }
    ]
    return schema


async def ingest(
    manifest: DatasetManifest,
    path: Path,
    services: AzureServices,
    allow_automated_review: bool = False,
) -> int:
    automated = (
        allow_automated_review
        and manifest.evidence_kind == "dataset_knowledge"
        and manifest.review_method == "automated"
    )
    if not manifest.license_reviewed or not (manifest.human_reviewed or automated):
        raise ValueError("License and human privacy review are mandatory before uploading")
    if automated and not manifest.human_reviewed:
        logger.warning(
            "Uploading an explicitly allowed automated-review pilot corpus, not expert advice"
        )
    if not services.settings.azure_search_endpoint:
        raise ValueError("AZURE_SEARCH_ENDPOINT is required")
    # Validate the complete file before the first remote mutation.
    raw = await asyncio.to_thread(path.read_text)
    documents = [
        SearchDocument.model_validate_json(line) for line in raw.splitlines() if line.strip()
    ]
    if not documents:
        raise ValueError("No documents to upload")
    for document in documents:
        if (
            document.dataset_id != manifest.dataset_id
            or document.evidence_kind != manifest.evidence_kind
            or document.license != manifest.license
            or document.updated_at != manifest.updated_at.isoformat()
        ):
            raise ValueError("Prepared document does not match the reviewed manifest")
        if redact(document.content) != document.content:
            raise ValueError("Prepared documents contain unredacted sensitive patterns")
        if urlparse(document.source_url).scheme != "https":
            raise ValueError("Invalid document provenance")
        if document.evidence_kind == "dataset_knowledge":
            if (
                document.source_file_key != manifest.source_file_key
                or not document.source_record_id
                or not document.source_display
                or document.review_status != manifest.review_method
            ):
                raise ValueError("Missing or mismatched dataset provenance/review")
    base = services.settings.azure_search_endpoint.rstrip("/")
    index = services.settings.azure_search_index
    headers = {"Authorization": f"Bearer {await services.token(search=True)}"}
    await services.request(
        "search",
        "PUT",
        f"{base}/indexes/{index}?api-version={SEARCH_API}",
        headers=headers,
        json=index_schema(index),
    )
    for start in range(0, len(documents), 100):
        batch = [
            {"@search.action": "mergeOrUpload", **document.model_dump()}
            for document in documents[start : start + 100]
        ]
        response = await services.request(
            "search",
            "POST",
            f"{base}/indexes/{index}/docs/index?api-version={SEARCH_API}",
            headers=headers,
            json={"value": batch},
        )
        failed = [row for row in response.json()["value"] if not row["status"]]
        if failed:
            raise ValueError(f"AI Search rejected {len(failed)} documents; ingestion is incomplete")
    return len(documents)


async def aihub_metadata(dataset_id: str | None = None) -> str:
    if dataset_id and not dataset_id.isdigit():
        raise ValueError("AI Hub dataset ID must be numeric")
    endpoint = f"/info/{dataset_id}.do" if dataset_id else "/info/dataset.do"
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        response = await client.get(AIHUB_BASE + endpoint)
        if not response.is_success:
            raise ValueError(
                f"AI Hub metadata HTTP {response.status_code}; no data was downloaded. "
                "Check AI Hub service status and dataset access."
            )
        return response.text


async def download_aihub(dataset_id: str, file_keys: str, output: Path, max_mb: int) -> int:
    if not dataset_id.isdigit() or not re.fullmatch(r"\d+(,\d+)*", file_keys):
        raise ValueError("Numeric dataset ID and explicit comma-separated file keys are required")
    if not 1 <= max_mb <= 1024:
        raise ValueError("max_mb must be between 1 and 1024")
    key = DataSourceSettings().aihub_api_key.get_secret_value()
    if not key:
        raise ValueError("AIHUB_API_KEY is required; the ambiguous API_KEY name is not supported")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = output.with_suffix(output.suffix + ".partial")
    size = 0
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            async with client.stream(
                "GET",
                f"{AIHUB_BASE}/down/0.6/{dataset_id}.do",
                params={"fileSn": file_keys},
                headers={"apikey": key},
            ) as initial:
                if initial.is_redirect:
                    target = urljoin(str(initial.url), initial.headers.get("location", ""))
                    url = urlparse(target)
                    if (
                        url.scheme != "https"
                        or url.hostname != "stream.aihub.or.kr"
                        or url.path != "/shellStream.do"
                        or url.username
                        or url.password
                        or url.port not in {None, 443}
                        or url.fragment
                    ):
                        raise ValueError("AI Hub returned an untrusted download redirect")
                    await initial.aclose()
                    # The signed stream URL authorizes this request; never forward the API key.
                    async with client.stream("GET", target) as response:
                        size = await save_download(response, partial, max_mb)
                else:
                    size = await save_download(initial, partial, max_mb)
        if not await asyncio.to_thread(tarfile.is_tarfile, partial):
            raise ValueError("AI Hub response is not a valid TAR archive")
        partial.replace(output)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise ValueError(f"AI Hub download connection failed ({type(exc).__name__})") from None
    except (ValueError, OSError):
        partial.unlink(missing_ok=True)
        raise
    return size


async def save_download(response: httpx.Response, partial: Path, max_mb: int) -> int:
    if not response.is_success:
        raise ValueError(
            f"AI Hub download HTTP {response.status_code}; check key and dataset approval"
        )
    if "json" in response.headers.get("content-type", ""):
        raise ValueError("AI Hub returned a status payload, not a dataset archive")
    size = 0
    with partial.open("wb") as handle:
        await asyncio.to_thread(partial.chmod, 0o600)
        async for block in response.aiter_bytes(65536):
            size += len(block)
            if size > max_mb * 1024 * 1024:
                raise ValueError("Download exceeded --max-mb; partial data removed")
            handle.write(block)
    if not size:
        raise ValueError("AI Hub returned an empty download")
    return size


async def run(args: argparse.Namespace) -> None:
    if args.command == "catalog":
        print(await aihub_metadata(args.dataset))
        return
    if args.command == "download":
        if not args.accept_license:
            raise ValueError("Read dataset terms first, then explicitly pass --accept-license")
        size = await download_aihub(args.dataset, args.files, args.output, args.max_mb)
        print(json.dumps({"downloaded_bytes": size, "path": str(args.output)}))
        return
    manifest = DatasetManifest.model_validate_json(args.manifest.read_text())
    if args.command == "prepare":
        print(json.dumps(prepare(manifest, args.input, args.output), ensure_ascii=False))
    elif args.command == "ingest":
        services = AzureServices(get_settings())
        try:
            count = await ingest(manifest, args.input, services, args.allow_automated_review)
            print(json.dumps({"uploaded_documents": count}))
        finally:
            await services.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Hub preparation and Azure AI Search ingestion")
    sub = parser.add_subparsers(dest="command", required=True)
    catalog = sub.add_parser("catalog", help="List dataset metadata or files without credentials")
    catalog.add_argument("--dataset")
    download = sub.add_parser("download", help="Download only explicitly selected, approved files")
    download.add_argument("--dataset", required=True)
    download.add_argument("--files", required=True)
    download.add_argument("--output", type=Path, required=True)
    download.add_argument(
        "--max-mb", type=int, default=100, choices=range(1, 1025), metavar="1..1024"
    )
    download.add_argument("--accept-license", action="store_true")
    prep = sub.add_parser("prepare")
    prep.add_argument("--manifest", type=Path, required=True)
    prep.add_argument("--input", type=Path)
    prep.add_argument("--output", type=Path, required=True)
    upload = sub.add_parser("ingest")
    upload.add_argument("--manifest", type=Path, required=True)
    upload.add_argument("--input", type=Path, required=True)
    upload.add_argument(
        "--allow-automated-review",
        action="store_true",
        help="Explicitly allow labeled, automatically screened dataset-knowledge pilot documents",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        parser.exit(1, f"Data operation failed: {exc}\n")


if __name__ == "__main__":
    main()
