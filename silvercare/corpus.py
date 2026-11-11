import argparse
import asyncio
import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from silvercare.azure import AzureServices, UpstreamError
from silvercare.config import get_settings
from silvercare.data import DatasetManifest, download_aihub, ingest, prepare


class CorpusSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dataset_id: str
    file_key: str
    purpose: Literal["knowledge", "qa", "dialogue", "evaluation"]
    raw_path: Path
    manifest_path: Path | None = None
    prepared_path: Path | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if not self.dataset_id.isdigit() or not self.file_key.isdigit():
            raise ValueError("Catalog dataset and file keys must be numeric")
        if self.purpose == "knowledge" and (
            self.manifest_path is None or self.prepared_path is None
        ):
            raise ValueError("Knowledge sources require manifest and prepared paths")
        return self

    def manifest(self) -> DatasetManifest:
        if self.manifest_path is None:
            raise ValueError("Evaluation sources are never prepared or indexed as knowledge")
        manifest = DatasetManifest.model_validate_json(self.manifest_path.read_text())
        if manifest.source_file_key != self.file_key:
            raise ValueError("Catalog and manifest file keys do not match")
        return manifest


class CorpusCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[CorpusSource]

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({source.name for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate corpus source names")
        if len({source.raw_path for source in self.sources}) != len(self.sources):
            raise ValueError("Multiple sources cannot overwrite the same raw path")
        return self


def receipt(source: CorpusSource, downloaded: bool) -> dict[str, object]:
    path = source.raw_path
    if not tarfile.is_tarfile(path):
        raise ValueError(f"{source.name}: raw input is not a valid TAR archive")
    return {
        "name": source.name,
        "dataset_id": source.dataset_id,
        "file_key": source.file_key,
        "purpose": source.purpose,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "downloaded": downloaded,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def write_receipts(rows: list[dict[str, object]]) -> None:
    path = Path("data/raw/aihub/registry.json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("w") as stream:
        path.chmod(0o600)
        json.dump({"sources": rows}, stream, ensure_ascii=False, indent=2)


async def run(args: argparse.Namespace) -> None:
    raw = await asyncio.to_thread(args.catalog.read_text)
    catalog = CorpusCatalog.model_validate_json(raw)
    if args.command == "list":
        print(catalog.model_dump_json(indent=2))
        return
    if args.command == "fetch":
        rows = []
        for source in catalog.sources:
            downloaded = args.refresh or not source.raw_path.exists()
            if downloaded:
                await download_aihub(
                    source.dataset_id, source.file_key, source.raw_path, args.max_mb
                )
            rows.append(await asyncio.to_thread(receipt, source, downloaded))
        await asyncio.to_thread(write_receipts, rows)
        print(json.dumps({"sources": rows}, ensure_ascii=False))
        return
    services = AzureServices(get_settings()) if args.command == "ingest" else None
    try:
        for source in catalog.sources:
            if source.purpose != "knowledge":
                continue
            manifest = await asyncio.to_thread(source.manifest)
            assert source.prepared_path is not None
            if args.command == "prepare":
                counts = await asyncio.to_thread(
                    prepare, manifest, source.raw_path, source.prepared_path
                )
                print(json.dumps({"source": source.name, **counts}, ensure_ascii=False))
            elif services is not None:
                count = await ingest(
                    manifest, source.prepared_path, services, args.allow_automated_review
                )
                print(json.dumps({"source": source.name, "indexed_documents": count}))
    finally:
        if services is not None:
            await services.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch, prepare and index the registered care corpus"
    )
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    fetch = commands.add_parser("fetch")
    fetch.add_argument(
        "--refresh", action="store_true", help="Redownload registered files explicitly"
    )
    fetch.add_argument("--max-mb", type=int, default=20)
    commands.add_parser("prepare")
    upload = commands.add_parser("ingest")
    upload.add_argument("--allow-automated-review", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, UpstreamError) as exc:
        parser.exit(1, f"Corpus operation failed: {exc}\n")


if __name__ == "__main__":
    main()
