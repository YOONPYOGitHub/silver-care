import json
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient
from tokenizers import Tokenizer, models, pre_tokenizers

from silvercare.foundry_gateway import FoundryGatewaySettings
from silvercare.foundry_gateway import create_gateway as gateway_factory


def encoder():
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    return tokenizer


def create_gateway(*args, **kwargs):
    return gateway_factory(*args, **kwargs, tokenizer=encoder())


def gateway_config():
    return FoundryGatewaySettings(
        _env_file=None,
        endpoint="https://models.services.ai.azure.com",
        llm_deployment="small",
        embedding_deployment="embedding",
    )


def auth():
    credential = AsyncMock()
    credential.get_token.return_value = AccessToken("synthetic-token", 9999999999)
    return credential


def test_chat_uses_only_configured_foundry_deployment_with_entra_auth():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/managed-deployments/small/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        payload = json.loads(request.content)
        assert payload["model"] == "small"
        assert payload["stream"] is False
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["messages"][0]["content"] == "일반 지식으로 답하세요."
        return httpx.Response(
            200,
            json={
                "model": "actual-small-model",
                "choices": [
                    {
                        "message": {"content": "설명입니다."},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with TestClient(
        create_gateway(
            gateway_config(), httpx.AsyncClient(transport=httpx.MockTransport(handler)), auth()
        )
    ) as client:
        result = client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3.5-4b",
                "messages": [{"role": "system", "content": "일반 지식으로 답하세요."}],
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        assert result.status_code == 200 and result.json()["model"] == "actual-small-model"
        assert (
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "arbitrary-azure-model",
                    "messages": [],
                },
            ).status_code
            == 422
        )
    assert len(calls) == 1


def test_generation_only_gateway_never_loads_or_calls_embedding(monkeypatch):
    def unexpected_tokenizer(*args):
        pytest.fail("Generation-only gateway must not load an embedding tokenizer")

    monkeypatch.setattr(Tokenizer, "from_file", unexpected_tokenizer)
    credential = auth()
    config = gateway_config().model_copy(update={"embedding_deployment": ""})
    with TestClient(
        gateway_factory(
            config,
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True}))
            ),
            credential,
        )
    ) as client:
        assert client.get("/health").status_code == 200
        assert (
            client.post("/embed", json={"inputs": ["text"], "truncate": False}).status_code == 404
        )
        credential.get_token.assert_not_called()
        result = client.post("/v1/chat/completions", json={"model": "qwen3.5-4b", "messages": []})
        assert result.status_code == 200
        assert credential.get_token.await_count == 1


@pytest.mark.parametrize("kind", ["query", "document"])
def test_embedding_preserves_document_text_and_adds_instruction_only_for_query(kind):
    def handler(request):
        assert request.url.path == "/managed-deployments/embedding/v1/embeddings"
        body = json.loads(request.content)
        assert body["model"] == "embedding" and body["dimensions"] == 1024
        assert body["input"][0].startswith("Instruct:") is (kind == "query")
        assert body["input"][0].endswith("낙상 예방")
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1] * 1024}],
                "usage": {"prompt_tokens": len(encoder().encode(body["input"][0]).ids)},
            },
        )

    with TestClient(
        create_gateway(
            gateway_config(), httpx.AsyncClient(transport=httpx.MockTransport(handler)), auth()
        )
    ) as client:
        r = client.post(
            "/embed",
            json={
                "inputs": ["낙상 예방"],
                "truncate": False,
                "input_type": kind,
            },
        )
        assert r.status_code == 200 and len(r.json()[0]) == 1024


@pytest.mark.parametrize(
    "data",
    [
        {"data": []},
        {"data": [{"index": 1, "embedding": [0.1] * 1024}]},
        {"data": [{"index": 0, "embedding": [0.1] * 8}]},
        {"data": [{"index": 0, "embedding": [True] * 1024}]},
    ],
)
def test_bad_upstream_embeddings_are_not_silently_used(data):
    with TestClient(
        create_gateway(
            gateway_config(),
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
            ),
            auth(),
        )
    ) as client:
        assert (
            client.post(
                "/embed",
                json={
                    "inputs": ["낙상 예방"],
                    "truncate": False,
                },
            ).status_code
            == 502
        )


def test_provider_refusal_is_preserved_without_leaking_prompt_or_upstream_details():
    with TestClient(
        create_gateway(
            gateway_config(),
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        400,
                        json={
                            "error": {"code": "content_filter", "message": "PRIVATE_UPSTREAM_TEXT"}
                        },
                    )
                )
            ),
            auth(),
        )
    ) as client:
        r = client.post("/v1/chat/completions", json={"model": "qwen3.5-4b", "messages": []})
        assert r.status_code == 400
        assert r.json() == {"error": {"code": "content_filter"}}
        assert "PRIVATE_UPSTREAM_TEXT" not in r.text


def test_oversized_or_truncating_embedding_requests_are_rejected_before_provider_call():
    with TestClient(
        create_gateway(
            gateway_config(),
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: pytest.fail("No provider request"))
            ),
            auth(),
        )
    ) as client:
        for payload in [
            {"inputs": ["text"], "truncate": True},
            {"inputs": ["text"] * 65, "truncate": False},
            {"inputs": [""], "truncate": False},
            {"inputs": ["word " * 16385], "truncate": False},
        ]:
            assert client.post("/embed", json=payload).status_code == 422


def test_provider_token_count_mismatch_rejects_silently_truncated_embeddings():
    with TestClient(
        create_gateway(
            gateway_config(),
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        json={
                            "data": [{"index": 0, "embedding": [0.1] * 1024}],
                            "usage": {"prompt_tokens": 1},
                        },
                    )
                )
            ),
            auth(),
        )
    ) as client:
        assert (
            client.post("/embed", json={"inputs": ["two words"], "truncate": False}).status_code
            == 502
        )
