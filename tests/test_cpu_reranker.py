import asyncio
from collections import OrderedDict
from types import SimpleNamespace

import httpx
import pytest

from silvercare.cpu_reranker import MAX_TOKENS, CpuReranker, create_app


def scorer(cache_size=8):
    np = pytest.importorskip("numpy")
    backend = CpuReranker.__new__(CpuReranker)
    backend.np = np
    backend.cache_size = cache_size
    backend.cache = OrderedDict()
    backend.hits = backend.misses = backend.inference_pairs = 0
    backend.pad_id = 1
    backend.input_names = {"input_ids", "attention_mask"}
    batches = []

    def encode(pairs):
        return [
            SimpleNamespace(ids=[len(text)] * len(text), type_ids=[0] * len(text))
            for _, text in pairs
        ]

    def run(outputs, feed):
        batches.append(feed["input_ids"].shape)
        return [np.asarray([[float(row[0]) / 10] for row in feed["input_ids"]])]

    backend.encoder = SimpleNamespace(encode_batch=encode)
    backend.session = SimpleNamespace(run=run)
    return backend, batches


def test_cpu_scores_preserve_input_order_deduplicate_and_cache():
    backend, batches = scorer()
    scores = backend.score("query", ["bbb", "a", "bbb", "cc"])
    assert scores[0] == scores[2] and scores[0] > scores[3] > scores[1]
    assert backend.inference_pairs == 3 and batches == [(3, 3)]
    assert backend.score("query", ["a", "bbb"]) == [scores[1], scores[0]]
    assert backend.inference_pairs == 3 and backend.hits == 2
    backend.score("another query", ["a"])
    assert backend.inference_pairs == 4


def test_cpu_cache_is_bounded_and_batch_padding_is_length_bucketed():
    backend, batches = scorer(cache_size=2)
    backend.score("query", ["a" * 10, "b" * 2000, "c" * 1000, "d" * 200])
    assert len(backend.cache) == 2
    assert all(size * width <= MAX_TOKENS for size, width in batches)
    with pytest.raises(ValueError, match="nontruncating"):
        backend.score("query", ["e" * (MAX_TOKENS + 1)])


def test_cpu_accepts_the_exact_context_limit_without_truncating():
    backend, batches = scorer()
    scores = backend.score("query", ["a" * MAX_TOKENS])
    assert len(scores) == 1
    assert batches == [(1, MAX_TOKENS)]
    assert backend.inference_pairs == 1


async def test_runtime_keeps_info_responsive_and_rejects_unsupported_options():
    backend = SimpleNamespace(
        manifest={"precision": "dynamic-int8"},
        cache={},
        hits=0,
        misses=0,
        inference_pairs=0,
        lock=asyncio.Lock(),
        slots=asyncio.Semaphore(16),
        score=lambda query, texts: [0.1, 0.9][: len(texts)],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://test"
    ) as client:
        info = (await client.get("/info")).json()
        assert info["max_input_length"] == 8192 and info["auto_truncate"] is False
        response = await client.post("/rerank", json={"query": "q", "texts": ["a", "b"]})
        assert response.json() == [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]
        for option in ("truncate", "raw_scores", "return_text"):
            assert (
                await client.post("/rerank", json={"query": "q", "texts": ["a"], option: True})
            ).status_code == 422
        assert (
            await client.post("/rerank", json={"query": "q", "texts": ["a"] * 65})
        ).status_code == 422
