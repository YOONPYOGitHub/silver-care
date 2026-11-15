import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from pydantic import BaseModel

from .clients import EvaluationSettings, Judge, collect_one
from .grading import PROMPT_SHA256, assess, claim_counts
from .models import Calibration, CaptureRun, Case, Dataset, Judgment, ServiceAnswer, fingerprint

logger = logging.getLogger("ondam_eval")


def load[T: BaseModel](path: Path, model: type[T]) -> T:
    return read_snapshot(path, model)[0]


def read_snapshot[T: BaseModel](path: Path, model: type[T]) -> tuple[T, str]:
    raw = path.read_bytes()
    return model.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def save_text(path: Path, text: str) -> None:
    if path.exists():
        raise ValueError(f"Refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save(path: Path, payload: object) -> None:
    save_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def export_review_sheet(dataset: Dataset, dataset_sha256: str, output: Path) -> None:
    stream = io.StringIO(newline="")
    columns = [
        "id",
        "category",
        "risk",
        "question",
        "history",
        "reference_answer",
        "required_criteria",
        "forbidden_criteria",
        "allowed_alternatives",
        "source_urls",
        "dataset_sha256",
        "review_status",
        "reviewer",
        "reviewed_at",
        "reference_corrections",
        "safety_concerns",
        "decision",
    ]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    sources = {source.id: str(source.url) for source in dataset.sources}
    for case in dataset.cases:
        ids = sorted({sid for c in case.required + case.forbidden for sid in c.source_ids})
        row = {
            "id": case.id,
            "category": case.category,
            "risk": case.risk,
            "question": case.question,
            "history": "\n".join(f"{m.role}: {m.content}" for m in case.history),
            "reference_answer": case.reference_answer,
            "required_criteria": "\n".join(
                f"{c.id} [critical={c.critical}]: {c.description}" for c in case.required
            ),
            "forbidden_criteria": "\n".join(
                f"{c.id} [critical={c.critical}]: {c.description}" for c in case.forbidden
            ),
            "allowed_alternatives": "\n".join(case.allowed_alternatives),
            "source_urls": "\n".join(sources[sid] for sid in ids),
            "dataset_sha256": dataset_sha256,
            "review_status": case.review_status,
        }
        writer.writerow(
            {
                key: "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
                for key, value in row.items()
            }
        )
    save_text(output, "\ufeff" + stream.getvalue())


def choose(dataset: Dataset, ids: list[str] | None, limit: int | None) -> list[Case]:
    if ids and not set(ids).issubset({case.id for case in dataset.cases}):
        raise ValueError("Unknown requested case ID")
    if limit is not None and limit < 1:
        raise ValueError("Limit must be positive")
    selected = [case for case in dataset.cases if not ids or case.id in ids]
    return selected[:limit] if limit is not None else selected


def require_review(cases: list[Case], allowed: bool) -> None:
    drafts = [case.id for case in cases if case.review_status != "expert_reviewed"]
    if drafts and not allowed:
        raise ValueError("Unreviewed cases: use --allow-unreviewed for PROVISIONAL results")
    if drafts:
        logger.warning(
            "%s cases are unreviewed: results are PROVISIONAL, not clinical validation", len(drafts)
        )


def base_report(dataset: Dataset, dataset_sha256: str, cases: list[Case]) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": dataset.id,
        "dataset_version": dataset.version,
        "dataset_sha256": dataset_sha256,
        "provisional": any(case.review_status != "expert_reviewed" for case in cases),
        "clinical_certification": False,
        "limitations": (
            "Reference-bound automated assessment; draft labels and judge calibration are not "
            "independent clinical validation. No evidence of safety outside tested scenarios."
        ),
    }


async def collect(
    args: argparse.Namespace, dataset: Dataset, settings: EvaluationSettings, dataset_hash: str
) -> int:
    cases = choose(dataset, args.ids, args.limit)
    require_review(cases, args.allow_unreviewed)
    if not settings.target_url:
        raise ValueError("Set EVAL_TARGET_URL or --target")
    records = []
    trace_token = settings.trace_token.get_secret_value() if args.trace else None
    if args.trace and (trace_token is None or len(trace_token) < 32):
        raise ValueError("Configure EVALUATION_TRACE_TOKEN before using --trace")
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        for index, case in enumerate(cases):
            if index:
                await asyncio.sleep(args.interval)
            record = await collect_one(client, settings.target_url, case, trace_token=trace_token)
            records.append(record)
            logger.info("%s: %s", case.id, record.outcome)
            if record.trace_status == "error":
                logger.error("%s: trace capture failed: %s", case.id, record.trace_error)
    run = CaptureRun(
        id=str(uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        dataset_sha256=dataset_hash,
        target_url=settings.target_url,
        target_label=args.target_label,
        records=records,
    )
    save(args.out, run.model_dump(mode="json"))
    return int(
        any(record.outcome != "answer" or record.trace_status == "error" for record in records)
    )


async def grade(
    args: argparse.Namespace, dataset: Dataset, settings: EvaluationSettings, dataset_hash: str
) -> int:
    run, capture_hash = read_snapshot(args.answers, CaptureRun)
    rubric_updated = run.dataset_sha256 != dataset_hash
    if rubric_updated and not args.allow_rubric_update:
        raise ValueError("Capture and grading dataset hashes differ; use the original dataset")
    if rubric_updated:
        logger.warning("Explicit rubric update: both dataset hashes will be recorded")
    by_id = {case.id: case for case in dataset.cases}
    if any(record.case_id not in by_id for record in run.records):
        raise ValueError("Capture contains unknown case IDs")
    for record in run.records:
        if record.request_sha256 != fingerprint(by_id[record.case_id].request_body()):
            raise ValueError(f"{record.case_id}: captured question/history does not match")
    cases = [by_id[record.case_id] for record in run.records]
    require_review(cases, args.allow_unreviewed)
    rows: list[dict[str, object]] = []
    async with (
        httpx.AsyncClient(timeout=settings.timeout_seconds) as client,
        DefaultAzureCredential(exclude_interactive_browser_credential=True) as credential,
    ):
        judge = Judge(settings, client, credential)
        for index, record in enumerate(run.records):
            case = by_id[record.case_id]
            row: dict[str, object] = {
                "case_id": case.id,
                "category": case.category,
                "risk": case.risk,
                "review_status": case.review_status,
                "service_outcome": record.outcome,
                "trace_status": record.trace_status,
                "trace_id": (
                    str(record.evaluation_trace.trace_id) if record.evaluation_trace else None
                ),
            }
            if record.answer is None:
                row.update(rating="not_answered", error=record.error)
            else:
                if index:
                    await asyncio.sleep(args.interval)
                try:
                    result = await judge.grade(case, dataset, record.answer)
                    rating = assess(case, dataset, record.answer, result)
                    row.update(
                        rating=rating,
                        claim_counts=claim_counts(result),
                        judgment=result.model_dump(mode="json"),
                    )
                except (ValueError, httpx.HTTPError, AzureError) as exc:
                    logger.error("%s: judge error: %s", case.id, type(exc).__name__)
                    row.update(rating="judge_error", error=f"{type(exc).__name__}: {exc}")
            logger.info("%s: %s", case.id, row["rating"])
            rows.append(row)
        report = base_report(dataset, dataset_hash, cases)
        report.update(
            capture_sha256=capture_hash,
            capture_dataset_sha256=run.dataset_sha256,
            rubric_updated_for_grading=rubric_updated,
            target_url=run.target_url,
            target_label=run.target_label,
            judge_deployment=settings.judge_deployment,
            actual_judge_models=sorted(judge.actual_models),
            prompt_sha256=PROMPT_SHA256,
            judgment_schema_sha256=fingerprint(Judgment.model_json_schema()),
            results=rows,
        )
    save(args.out, report)
    return int(any(row["rating"] != "pass" for row in rows))


async def calibrate(
    args: argparse.Namespace, dataset: Dataset, settings: EvaluationSettings, dataset_hash: str
) -> int:
    calibration, calibration_hash = read_snapshot(args.samples, Calibration)
    by_id = {case.id: case for case in dataset.cases}
    if any(sample.case_id not in by_id for sample in calibration.samples):
        raise ValueError("Calibration refers to unknown cases")
    samples = calibration.samples[: args.limit] if args.limit else calibration.samples
    if args.limit is not None and args.limit < 1:
        raise ValueError("Limit must be positive")
    cases = [by_id[sample.case_id] for sample in samples]
    require_review(cases, args.allow_unreviewed)
    rows: list[dict[str, object]] = []
    async with (
        httpx.AsyncClient(timeout=settings.timeout_seconds) as client,
        DefaultAzureCredential(exclude_interactive_browser_credential=True) as credential,
    ):
        judge = Judge(settings, client, credential)
        for index, sample in enumerate(samples):
            if index:
                await asyncio.sleep(args.interval)
            row: dict[str, object] = {
                "id": sample.id,
                "case_id": sample.case_id,
                "expected": sample.expected,
            }
            answer = ServiceAnswer(answer=sample.answer, mode="grounded")
            try:
                result = await judge.grade(by_id[sample.case_id], dataset, answer)
                rating = assess(by_id[sample.case_id], dataset, answer, result)
                row.update(
                    rating=rating,
                    agrees=rating == sample.expected,
                    claim_counts=claim_counts(result),
                    judgment=result.model_dump(mode="json"),
                )
            except (ValueError, httpx.HTTPError, AzureError) as exc:
                logger.error("%s: judge error: %s", sample.id, type(exc).__name__)
                row.update(rating="judge_error", agrees=False, error=f"{type(exc).__name__}: {exc}")
            logger.info("%s: expected=%s actual=%s", sample.id, sample.expected, row["rating"])
            rows.append(row)
        report = base_report(dataset, dataset_hash, cases)
        report.update(
            calibration_sha256=calibration_hash,
            judge_deployment=settings.judge_deployment,
            actual_judge_models=sorted(judge.actual_models),
            prompt_sha256=PROMPT_SHA256,
            agreement_count=sum(row["agrees"] is True for row in rows),
            sample_count=len(rows),
            unsafe_false_passes=sum(
                row["expected"] == "unsafe" and row["rating"] == "pass" for row in rows
            ),
            results=rows,
        )
    save(args.out, report)
    return int(any(row["agrees"] is not True for row in rows))


def render_report(path: Path, output: Path) -> None:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Expected a grading or calibration report")
    rows = data["results"]
    if any(not isinstance(row, dict) or not isinstance(row.get("rating"), str) for row in rows):
        raise ValueError("Invalid report result rows")
    counts = Counter(row["rating"] for row in rows)
    title = "PROVISIONAL / 전문가 미검토" if data.get("provisional") else "자동 평가 결과"
    lines = [
        f"# 온담 평가: {title}",
        "",
        f"- 전체 항목: {len(rows)} (오류·미응답도 분모에 포함)",
        f"- 평가 모델: {data.get('judge_deployment')}",
        f"- 실제 모델: {data.get('actual_judge_models')}",
        f"- 데이터셋 SHA256: {data.get('dataset_sha256')}",
        f"- 채점 프롬프트 SHA256: {data.get('prompt_sha256')}",
        "",
        "| 판정 | 건수 |",
        "| --- | ---: |",
        *[f"| {rating} | {count} |" for rating, count in sorted(counts.items())],
        "",
        "## 분야별 결과",
        "",
        "| 분야 | 전체 | pass | partial | fail | unsafe | needs_review | 오류·미응답 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for category in sorted({str(row.get("category", "calibration")) for row in rows}):
        group = [r for r in rows if str(r.get("category", "calibration")) == category]
        ratings = Counter(r["rating"] for r in group)
        cells = [str(ratings[r]) for r in ["pass", "partial", "fail", "unsafe", "needs_review"]]
        errors = sum(ratings[r] for r in ["judge_error", "not_answered"])
        lines.append(f"| {category} | {len(group)} | " + " | ".join(cells) + f" | {errors} |")
    lines += [
        "",
        "## 기준 답안 충족도와 추가 주장",
        "",
        "필수 내용 충족과 추가 주장의 검증 여부는 별개입니다. "
        "필수 내용을 모두 충족해도 근거 범위 밖 추가 주장은 검토가 필요할 수 있습니다.",
        "",
        "| 항목 | 필수 완전충족/전체 | 부분충족 | 누락 | 모순 | 불확실 | 미확인 추가주장 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        raw_judgment = row.get("judgment")
        if raw_judgment is None:
            lines.append(f"| {row.get('id', row.get('case_id'))} | 미채점 | - | - | - | - | - |")
        else:
            claim_stats = claim_counts(Judgment.model_validate(raw_judgment))
            values = [
                f"{claim_stats['required_met']}/{claim_stats['required_total']}",
                *[
                    str(claim_stats[f"required_{v}"])
                    for v in ["partial", "missing", "contradicted", "uncertain"]
                ],
                str(claim_stats["additional_unverified"]),
            ]
            lines.append(f"| {row.get('id', row.get('case_id'))} | " + " | ".join(values) + " |")
    lines += [
        "",
        "## 문항별 판정",
        "",
        "| 항목 | 판정 |",
        "| --- | --- |",
        *[f"| {row.get('id', row.get('case_id'))} | {row['rating']} |" for row in rows],
        "",
        "의학적 정확성·안전성 인증이 아닙니다. 상세 판정·인용문·오류는 원본 JSON을 확인하세요.",
        "평가 모델의 판단과 초안 정답 기준도 틀릴 수 있으며, 전문가 검토가 필요합니다.",
        "",
    ]
    if "agreement_count" in data:
        unsafe_rows = [row for row in rows if row.get("expected") == "unsafe"]
        lines += [
            "## 채점기 교정",
            "",
            f"- 기대 판정 일치: {data['agreement_count']}/{len(rows)}",
            f"- 위험 예시를 unsafe로 탐지: "
            f"{sum(row['rating'] == 'unsafe' for row in unsafe_rows)}/{len(unsafe_rows)}",
            f"- 위험 예시를 pass로 통과: {sum(row['rating'] == 'pass' for row in unsafe_rows)}",
            "",
            "이는 초안 테스트 라벨과의 일치이며 임상적 정확도나 전문가 검증 결과가 아닙니다.",
            "",
        ]
    save_text(output, "\n".join(lines))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Independent reference-bound Ondam evaluation")
    result.add_argument("--dataset", type=Path, required=True)
    result.add_argument("--env-file", type=Path)
    result.add_argument("--allow-unreviewed", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    review = commands.add_parser("review-sheet")
    review.add_argument("--out", type=Path, required=True)
    for name in ("collect", "grade", "calibrate"):
        command = commands.add_parser(name)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--interval", type=float, default=6)
        if name != "grade":
            command.add_argument("--limit", type=int)
        if name == "collect":
            command.add_argument("--target")
            command.add_argument("--target-label", default="unspecified")
            command.add_argument("--ids", nargs="+")
            command.add_argument("--trace", action="store_true")
        if name == "grade":
            command.add_argument("--answers", type=Path, required=True)
            command.add_argument("--allow-rubric-update", action="store_true")
        if name == "calibrate":
            command.add_argument("--samples", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--input", type=Path, required=True)
    report.add_argument("--out", type=Path, required=True)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parser().parse_args()
    try:
        if args.command == "report":
            render_report(args.input, args.out)
            return
        dataset, dataset_hash = read_snapshot(args.dataset, Dataset)
        if args.command == "review-sheet":
            export_review_sheet(dataset, dataset_hash, args.out)
            return
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "id": dataset.id,
                        "version": dataset.version,
                        "cases": len(dataset.cases),
                        "sources": len(dataset.sources),
                        "sha256": dataset_hash,
                        "categories": dict(Counter(c.category for c in dataset.cases)),
                        "review_status": dict(Counter(c.review_status for c in dataset.cases)),
                        "case_index": [
                            {"id": c.id, "category": c.category, "risk": c.risk}
                            for c in dataset.cases
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.out.exists():
            raise ValueError(f"Refusing to overwrite {args.out}")
        if not math.isfinite(args.interval) or args.interval < 0:
            raise ValueError("Interval must be finite and nonnegative")
        settings = EvaluationSettings(env_file=args.env_file)
        if args.command == "collect" and args.target:
            if args.trace and settings.target_url.rstrip("/") != args.target.rstrip("/"):
                raise ValueError("--trace target must match EVAL_TARGET_URL to protect its token")
            settings = EvaluationSettings(env_file=args.env_file, target_url=args.target)
        functions = {"collect": collect, "grade": grade, "calibrate": calibrate}
        runner = functions[args.command]
        raise SystemExit(asyncio.run(runner(args, dataset, settings, dataset_hash)))
    except (OSError, ValueError, httpx.HTTPError, AzureError) as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
