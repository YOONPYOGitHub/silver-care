import pytest

from silvercare.deploy import kubernetes_resources
from silvercare.deploy_comparison import MANAGED_MODELS, managed_body, resources, route_config


def test_comparison_resources_never_replace_baseline_or_host_the_llm_in_aks():
    rows = resources(
        "registry.test/experimental:1",
        "https://example.test",
        "client",
        "tenant",
        {"AZURE_SPEECH_ENDPOINT": "https://speech.test"},
        "https://models.services.ai.azure.com",
    )
    assert all(row["metadata"]["namespace"] == "silvercare-local" for row in rows)
    deployments = {r["metadata"]["name"]: r for r in rows if r["kind"] == "Deployment"}
    assert set(deployments) == {
        "foundry-gateway",
        "local-search",
        "local-reranker",
        "local-rag",
        "local-only",
    }
    for name in ("local-rag", "local-only", "foundry-gateway"):
        assert deployments[name]["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 660
    assert not any("llama" in str(row) or "local-model-prep" in str(row) for row in rows)
    reranker = deployments["local-reranker"]["spec"]["template"]["spec"]["containers"][0]
    assert reranker["args"][reranker["args"].index("--max-concurrent-requests") + 1] == "256"
    assert reranker["args"][reranker["args"].index("--max-client-batch-size") + 1] == "64"
    assert reranker["readinessProbe"]["httpGet"]["path"] == "/info"
    assert reranker["startupProbe"]["httpGet"]["path"] == "/info"
    configs = {r["metadata"]["name"]: r["data"] for r in rows if r["kind"] == "ConfigMap"}
    only = configs["local-only-config"]
    assert not any(key.startswith(("AZURE_SEARCH_", "LOCAL_SEARCH_", "KDCA_")) for key in only)
    assert only["BASE_PATH"] == "/local-only"
    assert configs["local-rag-config"]["LOCAL_SEARCH_ENDPOINT"] == "http://local-search:9200"
    assert configs["foundry-gateway-config"]["FOUNDRY_LLM_DEPLOYMENT"] == "ondam-qwen-4b"


def test_routes_keep_original_proxy_and_isolate_experimental_api_and_static_paths():
    rows = kubernetes_resources(
        "registry.test/baseline:1",
        "client",
        "tenant",
        "example.test",
        "https://search.test",
        "/resource/speech",
    )
    original = next(
        row["data"]["Caddyfile"]
        for row in rows
        if row["kind"] == "ConfigMap" and "Caddyfile" in row.get("data", {})
    )
    result = route_config(original)
    assert result.count("reverse_proxy silvercare:8000") == 1
    assert "handle_path /local-rag/*" in result
    assert "handle_path /local-only/*" in result
    assert "local-rag.silvercare-local.svc.cluster.local:8000" in result
    assert result.count("{") == result.count("}")
    assert "response_header_timeout 120s" in result
    assert result.count("response_header_timeout 660s") == 2
    assert result.count("header_up X-Real-IP {remote_host}") == 3
    with pytest.raises(ValueError):
        route_config(result)


def test_unknown_edge_config_fails_instead_of_overwriting_existing_routes():
    with pytest.raises(ValueError):
        route_config("an unexpected baseline config")


def test_managed_model_specifications_pin_small_models_and_use_template_labels():
    assert set(MANAGED_MODELS) == {"ondam-qwen-4b", "ondam-qwen-embedding"}
    for model, template in MANAGED_MODELS.values():
        body = managed_body(model, template)
        assert body["sku"] == {"name": "GlobalManagedCompute", "capacity": 1}
        assert "/versions/" in body["properties"]["model"]
        assert body["properties"]["deploymentTemplate"].endswith("/labels/latest")
