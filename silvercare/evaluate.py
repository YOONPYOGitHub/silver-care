import argparse
import asyncio
import hashlib
import io
import json
import re
import time
import unicodedata
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from silvercare.azure import SYSTEM_PROMPT, AzureServices, UpstreamError
from silvercare.config import get_settings
from silvercare.health import HealthUnavailable
from silvercare.models import ChatRequest
from silvercare.privacy import redact
from silvercare.tracing import EvaluationTrace


class SpeechSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    speaker_id: str = Field(min_length=1)
    age: int = Field(ge=60, le=120)
    seconds: float = Field(gt=0, le=45, allow_inf_nan=False)
    reference: str = Field(min_length=1, max_length=2000)


class SpeechManifest(BaseModel):
    source_url: str = ""
    samples: list[SpeechSample] = Field(min_length=1, max_length=50)


def speech_tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, 1):
        current = [row]
        for column, actual in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1]


async def evaluate_speech(path: Path, output: Path, base_url: str) -> dict[str, Any]:
    origin = urlsplit(base_url)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise ValueError("Speech evaluation requires a plain HTTPS service origin")
    raw = await asyncio.to_thread(path.read_bytes)
    manifest = SpeechManifest.model_validate_json(raw)
    samples = manifest.samples
    if len({row.audio for row in samples}) != len(samples):
        raise ValueError("Speech evaluation requires uniquely named audio samples")
    payloads: list[tuple[SpeechSample, bytes]] = []
    total_bytes = 0
    for sample in samples:
        if (
            Path(sample.audio).name != sample.audio
            or "\\" in sample.audio
            or Path(sample.audio).suffix.lower() != ".wav"
        ):
            raise ValueError("Speech sample must name a local WAV file without directories")
        audio = path.parent / sample.audio
        if await asyncio.to_thread(audio.is_symlink):
            raise ValueError("Speech evaluation does not accept symbolic links")
        size = (await asyncio.to_thread(audio.stat)).st_size
        if size > 4 * 1024 * 1024 or total_bytes + size > 64 * 1024 * 1024:
            raise ValueError("Speech evaluation exceeds the file or total byte budget")
        data = await asyncio.to_thread(audio.read_bytes)
        total_bytes += len(data)
        if len(data) != size:
            raise ValueError("Speech file changed during preflight")
        if hashlib.sha256(data).hexdigest() != sample.sha256:
            raise ValueError("Speech sample does not match its provenance checksum")
        with wave.open(io.BytesIO(data), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            if wav.getsampwidth() != 2 or not 0 < duration <= 45:
                raise ValueError("Speech evaluation requires 16-bit PCM WAV up to 45 seconds")
            if abs(duration - sample.seconds) > 0.01:
                raise ValueError("Speech duration does not match its manifest")
        if not speech_tokens(sample.reference) or redact(sample.reference) != sample.reference:
            raise ValueError("Speech reference is empty or contains a sensitive pattern")
        payloads.append((sample, data))
    report: dict[str, Any] = {
        "endpoint": base_url.rstrip("/"),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "source_url": manifest.source_url,
        "samples": [],
        "normalization": "Unicode NFKC and lowercase; punctuation ignored; CER ignores spaces.",
        "limitations": (
            "Small convenience sample, not a population-wide accuracy or usability certification. "
            "WER uses whitespace-delimited Korean units, not morphological analysis."
        ),
    }
    character_errors = word_errors = characters = words = 0
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=120) as client:
        for sample, data in payloads:
            started = time.perf_counter()
            response = await client.post(
                "/api/speech/transcribe",
                files={"audio": ("speech.wav", data, "audio/wav")},
            )
            response.raise_for_status()
            hypothesis = response.json().get("text")
            if not isinstance(hypothesis, str) or not hypothesis.strip():
                raise ValueError("Speech endpoint returned an empty or invalid transcription")
            reference_words = speech_tokens(sample.reference)
            actual_words = speech_tokens(hypothesis)
            reference_chars, actual_chars = "".join(reference_words), "".join(actual_words)
            char_errors = edit_distance(reference_chars, actual_chars)
            token_errors = edit_distance(reference_words, actual_words)
            character_errors += char_errors
            word_errors += token_errors
            characters += len(reference_chars)
            words += len(reference_words)
            report["samples"].append(
                {
                    "audio": sample.audio,
                    "sha256": sample.sha256,
                    "speaker_id": sample.speaker_id,
                    "age": sample.age,
                    "reference": sample.reference,
                    "hypothesis": hypothesis,
                    "character_errors": char_errors,
                    "word_errors": token_errors,
                    "seconds": sample.seconds,
                    "latency_seconds": round(time.perf_counter() - started, 3),
                }
            )
    report["metrics"] = {
        "samples": len(samples),
        "speakers": len({sample.speaker_id for sample in samples}),
        "ages": sorted({sample.age for sample in samples}),
        "audio_seconds": sum(sample.seconds for sample in samples),
        "reference_characters": characters,
        "character_errors": character_errors,
        "cer": character_errors / characters,
        "reference_words": words,
        "word_errors": word_errors,
        "wer": word_errors / words,
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    await asyncio.to_thread(output.write_text, json.dumps(report, ensure_ascii=False, indent=2))
    await asyncio.to_thread(output.chmod, 0o600)
    return report


class AudienceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    request: ChatRequest
    forbidden: list[str] = Field(default_factory=list)
    required_any: list[str] = Field(default_factory=list)
    required_concepts: list[list[str]] = Field(default_factory=list)
    accept_provider_safety_refusal: bool = False
    required_source_url: str = ""
    required_source_any: list[str] = Field(default_factory=list)
    required_model_source_urls: list[str] = Field(default_factory=list)
    required_source_type_groups: list[list[str]] = Field(default_factory=list)
    expected_mode: Literal["grounded", "conversation", "emergency"] | None = None
    required_patterns: list[str] = Field(default_factory=list)
    require_medical_caution: bool = False
    max_characters: int | None = Field(default=None, ge=1)

    @field_validator("required_concepts", "required_source_type_groups")
    @classmethod
    def meaningful_concepts(cls, groups: list[list[str]]) -> list[list[str]]:
        if any(not group or any(not term.strip() for term in group) for group in groups):
            raise ValueError("Required concept groups must contain nonempty alternatives")
        return groups

    @field_validator("required_patterns")
    @classmethod
    def valid_patterns(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            if not pattern.strip():
                raise ValueError("Required patterns must not be empty")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError("Required pattern is not a valid regular expression") from exc
        return patterns


async def evaluate_audience(path: Path, output: Path) -> dict[str, Any]:
    raw = await asyncio.to_thread(path.read_bytes)
    cases = [AudienceCase.model_validate(row) for row in json.loads(raw)["cases"]]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Audience evaluation requires nonempty, uniquely named cases")
    knowledge = await asyncio.to_thread(Path("data/processed/expanded/knowledge.jsonl").read_bytes)
    case_data = await asyncio.to_thread(Path("data/processed/expanded/cases.jsonl").read_bytes)
    services = AzureServices(get_settings())
    report: dict[str, Any] = {
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "scenarios_sha256": hashlib.sha256(raw).hexdigest(),
        "endpoint": services.settings.azure_search_endpoint,
        "index": services.settings.azure_search_index,
        "case_index": services.settings.azure_search_case_index,
        "official_health_enabled": bool(services.settings.kdca_health_api_key.get_secret_value()),
        "prefer_official_references": services.settings.prefer_official_references,
        "noncommercial_data_use": services.settings.noncommercial_data_use,
        "contextual_retrieval": services.settings.contextual_retrieval,
        "knowledge_sha256": hashlib.sha256(knowledge).hexdigest(),
        "cases_sha256": hashlib.sha256(case_data).hexdigest(),
        "cases": [],
        "limitations": (
            "Synthetic concept/citation regression checks, not clinical or human usability "
            "validation. Explicitly permitted provider safety refusals are not medical answers."
        ),
        "clinical_review_status": "not_performed",
    }
    try:
        for case in cases:
            trace = (
                EvaluationTrace.for_request(case.request)
                if case.required_model_source_urls
                else None
            )
            try:
                result = (
                    await services.chat(case.request, trace=trace)
                    if trace is not None
                    else await services.chat(case.request)
                )
            except (UpstreamError, HealthUnavailable) as exc:
                violation = (
                    f"upstream:{exc.service}:{exc.status}"
                    if isinstance(exc, UpstreamError)
                    else f"health:{exc.reason}"
                )
                accepted = (
                    case.accept_provider_safety_refusal
                    and isinstance(exc, UpstreamError)
                    and exc.service == "model"
                    and exc.status == 400
                    and exc.code == "content_filter"
                )
                report["cases"].append(
                    {
                        "id": case.id,
                        "violations": [] if accepted else [violation],
                        "passed": accepted,
                        "answer_generated": False,
                        "outcome": "provider_safety_refusal" if accepted else "upstream_error",
                        "provider_error": violation,
                    }
                )
                print(
                    json.dumps(
                        {"id": case.id, "passed": accepted, "error": violation, "answered": False}
                    ),
                    flush=True,
                )
                continue
            violations = [f"forbidden:{text}" for text in case.forbidden if text in result.answer]
            if case.required_any and not any(text in result.answer for text in case.required_any):
                violations.append("missing_target")
            for index, alternatives in enumerate(case.required_concepts):
                if not any(text.casefold() in result.answer.casefold() for text in alternatives):
                    violations.append(f"missing_concept:{index}")
            if case.max_characters is not None and len(result.answer) > case.max_characters:
                violations.append("answer_too_long")
            for index, pattern in enumerate(case.required_patterns):
                if not re.search(pattern, result.answer, re.MULTILINE):
                    violations.append(f"missing_pattern:{index}")
            if case.require_medical_caution and not result.medical_caution:
                violations.append("missing_medical_caution")
            source_urls = [source.url for source in result.sources]
            model_source_urls = [
                row["source_url"]
                for row in (trace.data.get("reference_identities", []) if trace else [])
                if row.get("source_url")
            ]
            if not set(case.required_model_source_urls).issubset(model_source_urls):
                violations.append("missing_required_model_source")
            source_types = {source.source_type for source in result.sources}
            if case.expected_mode is not None and result.mode != case.expected_mode:
                violations.append("unexpected_response_mode")
            for index, alternatives in enumerate(case.required_source_type_groups):
                if not source_types.intersection(alternatives):
                    violations.append(f"missing_source_type_group:{index}")
            if case.required_source_url and case.required_source_url not in source_urls:
                violations.append("missing_required_citation")
            if case.required_source_any and not set(case.required_source_any).intersection(
                source_urls
            ):
                violations.append("missing_required_source")
            row = {
                "id": case.id,
                "answer": result.answer,
                "violations": violations,
                "passed": not violations,
                "answer_generated": True,
                "outcome": "answer",
                "source_urls": source_urls,
                "model_source_urls": model_source_urls,
                "source_kinds": [source.evidence_kind for source in result.sources],
                "health_topic_ids": [topic.id for topic in result.health_topics],
                "mode": result.mode,
                "source_types": [source.source_type for source in result.sources],
                "source_jurisdictions": [source.jurisdiction for source in result.sources],
                "context_record_ids": [source.context_record_ids for source in result.sources],
                "medical_caution": result.medical_caution,
            }
            report["cases"].append(row)
            print(json.dumps({"id": case.id, "passed": row["passed"]}), flush=True)
    finally:
        await services.close()
    report["passed"] = all(row["passed"] for row in report["cases"])
    report["answers_generated"] = sum(row["answer_generated"] for row in report["cases"])
    report["provider_safety_refusals"] = sum(
        row["outcome"] == "provider_safety_refusal" for row in report["cases"]
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    await asyncio.to_thread(output.write_text, json.dumps(report, ensure_ascii=False, indent=2))
    await asyncio.to_thread(output.chmod, 0o600)
    return report


def relevant(row: dict[str, Any], target: dict[str, Any]) -> bool:
    return (
        row.get("source_url") in target.get("source_urls", [])
        or row.get("source_record_id") in target.get("record_ids", [])
        or row.get("dataset_id") in target.get("datasets", [])
        or any(text in row.get("title", "") for text in target.get("title_contains", []))
    )


async def evaluate(path: Path, output: Path) -> dict[str, Any]:
    raw = await asyncio.to_thread(path.read_text)
    queries = json.loads(raw)["queries"]
    if not queries:
        raise ValueError("Evaluation queries are empty")
    services = AzureServices(get_settings())
    report: dict[str, Any] = {
        "queries": [],
        "metrics": {},
        "top_k": 6,
        "hybrid_pipeline": {
            "rewrite_queries": services.settings.rewrite_queries,
            "semantic_ranking": services.settings.semantic_ranking,
            "prefer_official_references": services.settings.prefer_official_references,
            "noncommercial_data_use": services.settings.noncommercial_data_use,
            "contextual_retrieval": services.settings.contextual_retrieval,
        },
        "index": services.settings.azure_search_index,
        "endpoint": services.settings.azure_search_endpoint,
        "embedding_dimensions": services.settings.embedding_dimensions,
    }
    corpus = Path("data/processed/expanded/knowledge.jsonl")
    if await asyncio.to_thread(corpus.exists):
        content = await asyncio.to_thread(corpus.read_bytes)
        report["knowledge_sha256"] = hashlib.sha256(content).hexdigest()
    try:
        for target in queries:
            results: dict[str, Any] = {
                "query": target["query"],
                "required": target.get("required", False),
            }
            for mode in ("keyword", "hybrid"):
                started = time.perf_counter()
                query = (
                    await services.expand_query(target["query"], [])
                    if mode == "hybrid" and services.settings.rewrite_queries
                    else target["query"]
                )
                rows = await services.search(query, "reference", mode=mode)
                rank = next((i for i, row in enumerate(rows, 1) if relevant(row, target)), None)
                results[mode] = {
                    "hit": rank is not None,
                    "rank": rank,
                    "seconds": round(time.perf_counter() - started, 3),
                    "retrieval_query": query,
                    "results": [
                        {
                            "title": row["title"],
                            "record_id": row.get("source_record_id"),
                            "source_url": row.get("source_url"),
                            "excerpt": row["content"][:180],
                        }
                        for row in rows
                    ],
                }
            report["queries"].append(results)
            print(
                json.dumps(
                    {
                        "query": target["query"],
                        "keyword_rank": results["keyword"]["rank"],
                        "hybrid_rank": results["hybrid"]["rank"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        await services.close()
    for mode in ("keyword", "hybrid"):
        rows = [row[mode] for row in report["queries"]]
        report["metrics"][mode] = {
            "hit_at_6": sum(row["hit"] for row in rows) / len(rows),
            "mrr_at_6": sum(1 / row["rank"] if row["rank"] else 0 for row in rows) / len(rows),
            "mean_seconds": sum(row["seconds"] for row in rows) / len(rows),
        }
    report["required_targets_passed"] = all(
        row["hybrid"]["hit"] for row in report["queries"] if row["required"]
    )
    report["nonregression_passed"] = (
        report["metrics"]["hybrid"]["hit_at_6"] >= report["metrics"]["keyword"]["hit_at_6"]
        and report["metrics"]["hybrid"]["mrr_at_6"] >= report["metrics"]["keyword"]["mrr_at_6"]
        and report["required_targets_passed"]
    )
    report["limitations"] = (
        "Small fixed, non-exhaustive target set. Retrieval targets are not medical-answer accuracy."
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    await asyncio.to_thread(output.write_text, json.dumps(report, ensure_ascii=False, indent=2))
    await asyncio.to_thread(output.chmod, 0o600)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval, grounded audience responses or end-to-end speech"
    )
    parser.add_argument("--queries", type=Path, default=Path("data/retrieval-evaluation.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audience", action="store_true", help="Run audience response regressions")
    mode.add_argument("--speech-manifest", type=Path, help="Evaluate authorized elderly WAV pairs")
    parser.add_argument("--speech-base-url", help="HTTPS app origin for end-to-end STT evaluation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    name = (
        "speech-evaluation.json"
        if args.speech_manifest
        else "audience-evaluation.json"
        if args.audience
        else "retrieval-evaluation.json"
    )
    output = args.output or Path("data/processed/expanded") / name
    try:
        if args.speech_manifest:
            if not args.speech_base_url:
                parser.error("--speech-base-url is required with --speech-manifest")
            report = asyncio.run(
                evaluate_speech(args.speech_manifest, output, args.speech_base_url)
            )
            print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        elif args.audience:
            report = asyncio.run(evaluate_audience(args.queries, output))
            if not report["passed"]:
                parser.exit(1, "Audience response regression; inspect the saved report.\n")
        else:
            report = asyncio.run(evaluate(args.queries, output))
            print(json.dumps(report["metrics"], indent=2))
        if not args.audience and not args.speech_manifest and not report["nonregression_passed"]:
            parser.exit(1, "Hybrid target-hit rate regressed; investigate before release.\n")
    except (
        ValueError,
        OSError,
        UpstreamError,
        HealthUnavailable,
        httpx.HTTPError,
        wave.Error,
    ) as exc:
        parser.exit(1, f"Retrieval evaluation failed: {exc}\n")


if __name__ == "__main__":
    main()
