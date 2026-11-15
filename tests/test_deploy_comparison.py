import pytest

from silvercare.deploy import kubernetes_resources
from silvercare.deploy_comparison import (
    MANAGED_MODELS,
    managed_body,
    qwen_azure_resources,
    qwen_azure_route_config,
    resources,
    route_block,
    route_config,
)


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
    assert reranker["resources"]["limits"]["cpu"] == "6"
    assert reranker["resources"]["requests"]["memory"] == "3Gi"
    assert "@sha256:" in reranker["image"]
    assert "args" not in reranker
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
    assert "handle_path /qwen-azure-rag/*" in result
    assert "local-rag.silvercare-local.svc.cluster.local:8000" in result
    assert result.count("{") == result.count("}")
    assert "response_header_timeout 120s" in result
    assert result.count("response_header_timeout 660s") == 3
    assert result.count("header_up X-Real-IP {remote_host}") == 4
    existing = result.replace(route_block("qwen-azure-rag"), "")
    assert qwen_azure_route_config(existing) == result
    with pytest.raises(ValueError):
        qwen_azure_route_config(result)
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


def test_fourth_app_reuses_baseline_search_without_replacing_existing_deployments():
    baseline = {
        key: "configured"
        for key in (
            "AZURE_OPENAI_ENDPOINT AZURE_EMBEDDING_DEPLOYMENT AZURE_SEARCH_ENDPOINT "
            "AZURE_SEARCH_INDEX AZURE_SEARCH_CASE_INDEX RETRIEVAL_MODE EMBEDDING_DIMENSIONS "
            "SEMANTIC_RANKING REWRITE_QUERIES PREFER_OFFICIAL_REFERENCES CONTEXTUAL_RETRIEVAL "
            "NONCOMMERCIAL_DATA_USE PUBLIC_ORIGIN"
        ).split()
    }
    baseline.update(
        AZURE_OPENAI_DEPLOYMENT="baseline-gpt",
        EMBEDDING_DIMENSIONS="512",
        AZURE_SPEECH_RESOURCE_ID="shared-speech",
        KDCA_HEALTH_API_KEY="not-copied",
    )
    rows = qwen_azure_resources("registry.test/fourth:1", baseline)
    assert {row["kind"] for row in rows} == {"ConfigMap", "Deployment", "Service"}
    assert all(row["metadata"]["name"].startswith("qwen-azure-rag") for row in rows)
    config = next(row["data"] for row in rows if row["kind"] == "ConfigMap")
    assert config["AZURE_SEARCH_INDEX"] == baseline["AZURE_SEARCH_INDEX"]
    assert config["AZURE_SPEECH_RESOURCE_ID"] == "shared-speech"
    assert config["EMBEDDING_DIMENSIONS"] == "512"
    assert config["SERVICE_VARIANT"] == "qwen-azure-rag"
    assert "AZURE_OPENAI_DEPLOYMENT" not in config and "KDCA_HEALTH_API_KEY" not in config
    assert not any(
        key.startswith(("LOCAL_SEARCH_", "LOCAL_EMBEDDING_", "LOCAL_RERANKER_")) for key in config
    )
    del baseline["AZURE_EMBEDDING_DEPLOYMENT"]
    with pytest.raises(ValueError):
        qwen_azure_resources("registry.test/fourth:1", baseline)
