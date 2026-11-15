"""Pinned BGE CPU inference, nontruncating batches and bounded score-only cache."""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from tokenizers import Tokenizer  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)
MODEL = "woxpas-ai/bge-reranker-v2-m3-onnx"
REVISION = "c44ebc43de724ae8816668bb44d2e728e17faa18"
WEIGHTS_SHA256 = "1ed01a24f6e639dbd0a18e74e47b394abb78e6adb13dd23f34f94a79623fb3d3"
ARTIFACTS = {
    "float32": {
        "onnx/model.onnx": "80fbd49e823d0b385f8a3fa3e8b72180750f83bca94609b902b1ea632b10046c",
        "onnx/model.onnx_data": "9a748c82efb2079d24650c489e053dbb3c71d8acbbcf04d7b2340db66f2748f7",
    },
    "dynamic-int8": {"onnx/model_quantized.onnx": WEIGHTS_SHA256},
}
MAX_TOKENS = 8192


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=16000)
    texts: list[str] = Field(min_length=1, max_length=64)
    truncate: bool = False
    raw_scores: bool = False
    return_text: bool = False


def prepare(output: Path, precision: str = "float32") -> None:
    if precision not in ARTIFACTS:
        raise ValueError("Unsupported reranker precision")
    output.mkdir(parents=True, exist_ok=False)
    for filename in (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        *ARTIFACTS[precision],
    ):
        target = output / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{filename}", target
        )
    for filename, digest in ARTIFACTS[precision].items():
        with (output / filename).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Pinned reranker artifact hash mismatch")
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "model": MODEL,
                "revision": REVISION,
                "precision": precision,
                "artifacts": ARTIFACTS[precision],
                "max_tokens": MAX_TOKENS,
            }
        )
    )


class CpuReranker:
    def __init__(self, path: Path, *, threads: int = 6, cache_size: int = 2048) -> None:
        import numpy as np
        import onnxruntime as ort  # type: ignore[import-untyped]

        self.np = np
        if not 1 <= threads <= 8 or not 1 <= cache_size <= 8192:
            raise ValueError("Invalid bounded CPU/cache settings")
        self.cache_size = cache_size
        self.cache: OrderedDict[str, float] = OrderedDict()
        self.hits = self.misses = self.inference_pairs = 0
        self.manifest = json.loads((path / "manifest.json").read_text())
        precision = self.manifest.get("precision")
        if (
            self.manifest.get("revision") != REVISION
            or precision not in ARTIFACTS
            or self.manifest.get("artifacts") != ARTIFACTS[precision]
        ):
            raise ValueError("Unexpected reranker artifact manifest")
        self.encoder = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.encoder.no_truncation()
        self.encoder.no_padding()
        config = json.loads((path / "config.json").read_text())
        self.pad_id = config["pad_token_id"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(
            str(path / next(iter(ARTIFACTS[precision]))),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        if not {"input_ids", "attention_mask"} <= self.input_names or self.input_names - {
            "input_ids",
            "attention_mask",
            "token_type_ids",
        }:
            raise ValueError("Unsupported pinned ONNX input schema")
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(16)
        self.score("warmup", ["warmup"])

    def score(self, query: str, texts: list[str]) -> list[float]:
        keys = [
            hashlib.sha256(json.dumps([query, text], ensure_ascii=False).encode()).hexdigest()
            for text in texts
        ]
        missing: dict[str, str] = {}
        scores: dict[str, float] = {}
        for key, text in zip(keys, texts, strict=True):
            if key in self.cache:
                self.hits += 1
                self.cache.move_to_end(key)
                scores[key] = self.cache[key]
            else:
                self.misses += 1
                missing.setdefault(key, text)
        encodings = self.encoder.encode_batch([(query, text) for text in missing.values()])
        if any(len(item.ids) > MAX_TOKENS for item in encodings):
            raise ValueError("Reranking input exceeds the nontruncating 8192-token limit")
        work = sorted(zip(missing, encodings, strict=True), key=lambda item: len(item[1].ids))
        offset = 0
        while offset < len(work):
            end = offset + 1
            while (
                end < len(work)
                and end - offset < 8
                and len(work[end][1].ids) * (end + 1 - offset) <= MAX_TOKENS
            ):
                end += 1
            batch = work[offset:end]
            width = max(len(item.ids) for _, item in batch)
            ids = self.np.full((len(batch), width), self.pad_id, dtype=self.np.int64)
            mask = self.np.zeros_like(ids)
            types = self.np.zeros_like(ids)
            for index, (_, encoding) in enumerate(batch):
                length = len(encoding.ids)
                ids[index, :length] = encoding.ids
                mask[index, :length] = 1
                types[index, :length] = encoding.type_ids
            feed = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
            values = self.session.run(None, {key: feed[key] for key in self.input_names})[0]
            if values.shape != (len(batch), 1):
                raise RuntimeError("Unexpected ONNX reranker output shape")
            for (key, _), value in zip(batch, values[:, 0], strict=True):
                logit = float(value)
                if not math.isfinite(logit):
                    raise RuntimeError("Nonfinite reranker output")
                score = (
                    1 / (1 + math.exp(-logit))
                    if logit >= 0
                    else (math.exp(logit) / (1 + math.exp(logit)))
                )
                scores[key] = score
                self.cache[key] = score
                self.cache.move_to_end(key)
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
            self.inference_pairs += len(batch)
            offset = end
        return [scores[key] for key in keys]


def create_app(reranker: CpuReranker | None = None) -> FastAPI:
    backend = reranker or CpuReranker(
        Path(os.getenv("CPU_RERANKER_PATH", "/app/models/bge-reranker-cpu"))
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/info")
    @app.get("/health")
    async def info() -> dict[str, Any]:
        return {
            **backend.manifest,
            "max_input_length": MAX_TOKENS,
            "auto_truncate": False,
            "cache_entries": len(backend.cache),
            "cache_hits": backend.hits,
            "cache_misses": backend.misses,
            "inference_pairs": backend.inference_pairs,
        }

    @app.post("/rerank")
    async def rerank(payload: RerankRequest) -> list[dict[str, int | float]]:
        if payload.truncate or payload.raw_scores or payload.return_text:
            raise HTTPException(
                422, "Only nontruncating normalized score-only requests are supported."
            )
        if backend.slots.locked():
            raise HTTPException(429, "CPU reranker queue is full")
        async with backend.slots, backend.lock:
            try:
                scores = await asyncio.to_thread(backend.score, payload.query, payload.texts)
            except ValueError:
                logger.warning("CPU reranker rejected an invalid or oversized input")
                raise HTTPException(422, "Invalid or oversized reranking input") from None
            except RuntimeError:
                logger.exception("CPU reranker inference failed")
                raise HTTPException(503, "CPU reranker inference failed") from None
        return sorted(
            [{"index": index, "score": score} for index, score in enumerate(scores)],
            key=lambda row: -row["score"],
        )

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--precision", choices=list(ARTIFACTS), default="float32")
    args = parser.parse_args()
    prepare(args.output, args.precision)
