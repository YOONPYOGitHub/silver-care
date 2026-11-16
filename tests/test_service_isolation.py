import copy

import pytest

from silvercare import deploy_comparison, service_isolation
from silvercare.service_isolation import (
    PROFILES,
    application_resources,
    bindings,
    gateway_resources,
    network_policy,
    validate_bindings,
)


def state():
    result = {"original_foundry_id": "/accounts/v4", "profiles": {}}
    for name, plan in PROFILES.items():
        number = plan["number"]
        result["profiles"][name] = {
            **plan,
            "account_id": f"/accounts/v{number}",
            "client_id": f"client-{number}",
            "service_account": f"silvercare-v{number}",
            "image": f"unchanged-image-{number}",
            "speech_endpoint": f"https://v{number}.cognitiveservices.azure.com",
            "endpoint": f"https://v{number}.services.ai.azure.com",
            "openai_endpoint": f"https://v{number}.openai.azure.com",
            "search_endpoint": f"https://v{number}.search.windows.net",
            "config_name": "original-config",
            "original_config": {
                "LOCAL_LLM_ENDPOINT": "http://shared:8080",
                "LOCAL_EMBEDDING_ENDPOINT": "http://shared:8080",
                "PUBLIC_ORIGIN": "https://unchanged.example",
                "BASE_PATH": "" if name == "azure" else "/" + name,
                "LOCAL_RERANKER_ENDPOINT": "http://local-reranker:8080",
            },
            "original_spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "image": "old",
                                "envFrom": [{"configMapRef": {"name": "original-config"}}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/health/live", "port": 8000}
                                },
                            }
                        ]
                    }
                },
            },
        }
    return result


def test_each_profile_has_separate_runtime_bindings():
    validate_bindings(state())


@pytest.mark.parametrize(
    "field",
    [
        "pool",
        "account_id",
        "client_id",
        "speech_endpoint",
        "service_account",
        "search_endpoint",
    ],
)
def test_shared_bindings_are_rejected(field):
    config = state()
    config["profiles"]["azure"][field] = config["profiles"]["qwen-azure-rag"][field]
    with pytest.raises(ValueError):
        validate_bindings(config)


@pytest.mark.parametrize("name", ["local-only", "qwen-azure-rag"])
def test_generation_only_gateways_cannot_target_shared_embedding(name):
    profile = state()["profiles"][name]
    config, service, deployment = gateway_resources(name, profile, "pinned-image")
    assert config["data"]["FOUNDRY_EMBEDDING_DEPLOYMENT"] == ""
    assert config["data"]["FOUNDRY_ENDPOINT"] == profile["endpoint"]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == profile["service_account"]
    assert pod["nodeSelector"] == {"workload": profile["workload"]}
    assert service["spec"]["selector"] == {"app": profile["app"] + "-foundry"}


def test_local_rag_keeps_its_own_embedding_and_unchanged_reranker():
    profile = state()["profiles"]["local-rag"]
    config = gateway_resources("local-rag", profile, "pinned-image")[0]["data"]
    assert config["FOUNDRY_EMBEDDING_DEPLOYMENT"] == "ondam-qwen-embedding"
    new = bindings("local-rag", profile)
    assert new["LOCAL_LLM_ENDPOINT"] == new["LOCAL_EMBEDDING_ENDPOINT"]
    assert new["LOCAL_RERANKER_ENDPOINT"] == profile["original_config"]["LOCAL_RERANKER_ENDPOINT"]
    assert new["AZURE_SEARCH_ENDPOINT"] == ""


@pytest.mark.parametrize("name", PROFILES)
def test_canary_preserves_image_and_url_but_cannot_receive_public_service_traffic(name):
    profile = state()["profiles"][name]
    original = copy.deepcopy(profile)
    config, deployment = application_resources(name, profile, canary=True)
    pod = deployment["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == profile["image"]
    assert config["data"]["BASE_PATH"] == profile["original_config"]["BASE_PATH"]
    assert config["data"]["PUBLIC_ORIGIN"] == profile["original_config"]["PUBLIC_ORIGIN"]
    assert deployment["spec"]["selector"]["matchLabels"]["app"] != profile["app"]
    assert config["metadata"]["namespace"] == profile["namespace"]
    assert profile == original


def test_network_policy_only_admits_same_profile_and_shared_edge():
    profile = state()["profiles"]["local-rag"]
    spec = network_policy("local-rag", profile)["spec"]
    assert spec["podSelector"]["matchLabels"] == {"service-profile": "local-rag"}
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert spec["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {
        "service-profile": "local-rag"
    }
    assert spec["ingress"][1]["ports"] == [{"port": 8000}]
    public = spec["egress"][2]
    assert "169.254.0.0/16" in public["to"][0]["ipBlock"]["except"]
    assert public["ports"] == [{"port": 443}, {"port": 80}]


def test_legacy_deployment_is_blocked_by_authoritative_cluster_marker(tmp_path, monkeypatch):
    (tmp_path / "kubeconfig").touch()
    monkeypatch.setattr(deploy_comparison, "RUNTIME", tmp_path)
    monkeypatch.setattr(deploy_comparison, "kubectl", lambda *args: '{"data":{"mode":"dedicated"}}')
    with pytest.raises(ValueError, match="Shared deployment commands are blocked"):
        deploy_comparison.reject_shared_deployment()


def test_legacy_deployment_remains_available_before_migration(tmp_path, monkeypatch):
    (tmp_path / "kubeconfig").touch()
    monkeypatch.setattr(deploy_comparison, "RUNTIME", tmp_path)
    monkeypatch.setattr(deploy_comparison, "kubectl", lambda *args: "")
    deploy_comparison.reject_shared_deployment()


def test_snapshot_cannot_reconstruct_unsafe_original_bindings_after_migration(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service_isolation, "STATE", tmp_path / "missing-state.json")

    def blocked():
        raise ValueError("Existing isolated runtime requires its saved state")

    monkeypatch.setattr(service_isolation, "reject_shared_deployment", blocked)
    with pytest.raises(ValueError, match="requires its saved state"):
        service_isolation.snapshot()


def test_finalized_migration_cannot_restart_backing_services(monkeypatch):
    config = state()
    config["activated"] = True
    monkeypatch.setattr(service_isolation, "load", lambda: config)
    monkeypatch.setattr(service_isolation, "kubectl", lambda *args: pytest.fail("No mutations"))
    with pytest.raises(ValueError, match="already finalized"):
        service_isolation.finalize()


def test_tagged_image_is_rejected_before_any_runtime_read(monkeypatch):
    monkeypatch.setattr(service_isolation, "load", lambda: pytest.fail("No runtime access"))
    with pytest.raises(ValueError, match="immutable image digest"):
        service_isolation.deploy_image("local-rag", "registry/app:latest", "app")
