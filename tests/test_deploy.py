import json
import subprocess
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from silvercare import deploy
from silvercare.config import Settings
from silvercare.deploy import command, kubernetes_resources, verify_public_url


def test_public_tls_workload_identity_and_network_isolation():
    resources = kubernetes_resources(
        "registry/app:v1",
        "client-id",
        "tenant",
        "care.koreacentral.cloudapp.azure.com",
        "https://search.example",
        "/speech/resource",
    )
    edge = next(
        r
        for r in resources
        if r["kind"] == "Service" and r["metadata"]["name"] == "silvercare-edge"
    )
    assert edge["spec"]["type"] == "LoadBalancer"
    assert [p["port"] for p in edge["spec"]["ports"]] == [80, 443]
    app = next(
        r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "silvercare"
    )
    assert app["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert app["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"]
    assert any(r["kind"] == "NetworkPolicy" for r in resources)
    assert not any(r["kind"] == "Secret" for r in resources)
    app_env = app["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert all("secretRef" not in entry for entry in app_env)
    edge_deployment = next(
        r
        for r in resources
        if r["kind"] == "Deployment" and r["metadata"]["name"] == "silvercare-edge"
    )
    edge_security = edge_deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]
    assert edge_security["capabilities"] == {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]}
    service_account = next(r for r in resources if r["kind"] == "ServiceAccount")
    assert (
        service_account["metadata"]["annotations"]["azure.workload.identity/client-id"]
        == "client-id"
    )


def test_public_readiness_waits_for_real_https(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("certificate pending")
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("silvercare.deploy.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("silvercare.deploy.time.sleep", lambda seconds: None)
    verify_public_url("https://care.example")
    assert len(calls) == 2


def test_health_credential_is_only_in_secret_not_configmap_or_plain_container_env():
    resources = kubernetes_resources(
        "registry/app:v1",
        "client-id",
        "tenant",
        "care.example",
        "https://search.example",
        "/speech/resource",
        kdca_key=SecretStr("synthetic-health"),
    )
    secret = next(row for row in resources if row["kind"] == "Secret")
    assert secret["stringData"] == {"KDCA_HEALTH_API_KEY": "synthetic-health"}
    for config in [row for row in resources if row["kind"] == "ConfigMap"]:
        assert "synthetic-health" not in str(config)
    app = next(
        row
        for row in resources
        if row["kind"] == "Deployment" and row["metadata"]["name"] == "silvercare"
    )
    container = app["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [
        {
            "name": "KDCA_HEALTH_API_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": "silvercare-health", "key": "KDCA_HEALTH_API_KEY"}
            },
        }
    ]
    assert "synthetic-health" not in str(app)


def test_stdin_command_failure_does_not_echo_secret_manifests(monkeypatch):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 1, stdout="synthetic-secret", stderr="invalid Secret: synthetic-secret"
        )

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        command("kubectl", "apply", "-f", "-", data='{"secret":"synthetic-secret"}')
    assert "synthetic-secret" not in str(error.value)
    assert "withheld" in str(error.value)


def test_evaluation_trace_credential_has_its_own_secret_and_defaults_off():
    token = SecretStr("synthetic-evaluation-only-token-0123456789")
    resources = kubernetes_resources(
        "registry/app:v1",
        "client",
        "tenant",
        "care.example",
        "https://search.example",
        "/speech",
        evaluation_trace_enabled=True,
        evaluation_trace_token=token,
    )
    secret = next(r for r in resources if r["metadata"]["name"] == "silvercare-evaluation")
    assert secret["stringData"] == {"EVALUATION_TRACE_TOKEN": token.get_secret_value()}
    config = next(r for r in resources if r["kind"] == "ConfigMap")
    assert config["data"]["EVALUATION_TRACE_ENABLED"] == "true"
    assert token.get_secret_value() not in str(config)
    deployment = next(
        r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "silvercare"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert token.get_secret_value() not in str(container)
    assert container["env"] == [
        {
            "name": "EVALUATION_TRACE_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "silvercare-evaluation",
                    "key": "EVALUATION_TRACE_TOKEN",
                }
            },
        }
    ]
    with pytest.raises(ValueError, match="separate token"):
        kubernetes_resources(
            "image",
            "client",
            "tenant",
            "care.example",
            "https://search.example",
            "/speech",
            evaluation_trace_enabled=True,
        )


@pytest.mark.parametrize("failure", ["rollout", "https", None])
def test_deployment_state_is_written_only_after_verified_readiness(tmp_path, monkeypatch, failure):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    previous = {"image": "last-ready", "url": "https://previous.example"}
    state = runtime / "deployment.json"
    state.write_text(json.dumps(previous))
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "RUNTIME", runtime)
    monkeypatch.setattr(deploy, "role", lambda *args: None)
    monkeypatch.setattr(deploy, "seed_reference", AsyncMock())
    monkeypatch.setattr(deploy, "Settings", lambda **kwargs: Settings(_env_file=None))
    outputs = {
        "identityPrincipalId": "app",
        "searchId": "/search",
        "kubeletPrincipalId": "kubelet",
        "registryId": "/registry",
        "searchEndpoint": "https://search.example",
        "registryServer": "registry.example",
        "clusterName": "cluster",
        "identityClientId": "client",
    }

    def az_json(*args):
        if args[:2] == ("deployment", "group"):
            return {key: {"value": value} for key, value in outputs.items()}
        if args[:2] == ("account", "show"):
            return {"id": "12345678-subscription", "tenantId": "tenant"}
        return "/synthetic/resource"

    def fake_command(*args, **kwargs):
        if "get-credentials" in args:
            (runtime / "kubeconfig").write_text("synthetic configuration")
        if "rollout" in args and failure == "rollout":
            raise RuntimeError("rollout not ready")
        return ""

    def verify(url):
        assert json.loads(state.read_text()) == previous
        if failure == "https":
            raise RuntimeError("HTTPS not ready")

    monkeypatch.setattr(deploy, "az_json", az_json)
    monkeypatch.setattr(deploy, "command", fake_command)
    monkeypatch.setattr(deploy, "verify_public_url", verify)
    if failure:
        with pytest.raises(RuntimeError):
            deploy.deploy(skip_infra=True, skip_build=True, tag="synthetic")
        assert json.loads(state.read_text()) == previous
    else:
        deploy.deploy(skip_infra=True, skip_build=True, tag="synthetic")
        assert json.loads(state.read_text())["image"] == "registry.example/silvercare:synthetic"
        assert state.stat().st_mode & 0o777 == 0o600
