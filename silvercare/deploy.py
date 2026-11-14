"""Deploy the AKS app with retrieval release settings and server-only data-source secrets."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import SecretStr

from silvercare.azure import AzureServices, UpstreamError
from silvercare.config import Settings
from silvercare.data import DatasetManifest, ingest, prepare

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
GROUP = "rg-silver-care"
LOCATION = "koreacentral"
NAMESPACE = "silvercare"
FOUNDRY_NAME = "ai-foundry-m098047"
FOUNDRY_GROUP = "RG-AI"


def command(*args: str, data: str | None = None) -> str:
    result = subprocess.run(args, input=data, text=True, capture_output=True, check=False)
    if result.returncode:
        detail = (
            "stderr withheld because stdin may contain secrets"
            if data is not None
            else result.stderr[-3000:]
        )
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}):\n{detail}")
    return result.stdout.strip()


def az_json(*args: str) -> Any:
    return json.loads(command("az", *args, "-o", "json"))


def role(principal: str, name: str, scope: str, principal_type: str = "ServicePrincipal") -> None:
    existing = az_json(
        "role",
        "assignment",
        "list",
        "--scope",
        scope,
        "--query",
        f"[?principalId=='{principal}' && roleDefinitionName=='{name}'].id",
    )
    if existing:
        return
    command(
        "az",
        "role",
        "assignment",
        "create",
        "--assignee-object-id",
        principal,
        "--assignee-principal-type",
        principal_type,
        "--role",
        name,
        "--scope",
        scope,
        "--name",
        str(uuid5(NAMESPACE_URL, f"{scope}/{principal}/{name}")),
        "--output",
        "none",
    )


async def seed_reference(endpoint: str) -> None:
    raw = await asyncio.to_thread((ROOT / "data/reference.json").read_text)
    manifest = DatasetManifest.model_validate_json(raw)
    output = ROOT / "data/processed/reference.jsonl"
    await asyncio.to_thread(prepare, manifest, None, output)
    services = AzureServices(Settings(azure_search_endpoint=endpoint))
    try:
        for attempt in range(6):
            try:
                count = await ingest(manifest, output, services)
                print(f"Official reference documents indexed: {count}", flush=True)
                return
            except UpstreamError as exc:
                if exc.status != 403 or attempt == 5:
                    raise
                print("Search role assignment is propagating; retrying in 20s.", flush=True)
                await asyncio.sleep(20)
    finally:
        await services.close()


def resource(kind: str, name: str, **fields: Any) -> dict[str, Any]:
    versions = {
        "Deployment": "apps/v1",
        "NetworkPolicy": "networking.k8s.io/v1",
    }
    return {
        "apiVersion": versions.get(kind, "v1"),
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE},
        **fields,
    }


def kubernetes_resources(
    image: str,
    client_id: str,
    tenant: str,
    host: str,
    search_endpoint: str,
    speech_resource_id: str,
    retrieval: dict[str, str] | None = None,
    kdca_key: SecretStr | None = None,
) -> list[dict[str, Any]]:
    config = {
        "APP_ENV": "production",
        "PUBLIC_ORIGIN": f"https://{host}",
        "AZURE_OPENAI_ENDPOINT": f"https://{FOUNDRY_NAME}.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-6.1-terra",
        "AZURE_SEARCH_ENDPOINT": search_endpoint,
        "AZURE_SEARCH_INDEX": "silvercare",
        "AZURE_SPEECH_ENDPOINT": f"https://{FOUNDRY_NAME}.cognitiveservices.azure.com",
        "AZURE_SPEECH_RESOURCE_ID": speech_resource_id,
        "AZURE_SPEECH_REGION": LOCATION,
        "TRUSTED_PROXY": "true",
        "RATE_LIMIT_PER_MINUTE": "24",
    }
    if retrieval is not None:
        allowed = {
            "AZURE_SEARCH_ENDPOINT",
            "AZURE_SEARCH_INDEX",
            "AZURE_SEARCH_CASE_INDEX",
            "RETRIEVAL_MODE",
            "AZURE_EMBEDDING_DEPLOYMENT",
            "EMBEDDING_DIMENSIONS",
            "REWRITE_QUERIES",
            "SEMANTIC_RANKING",
        }
        if not allowed <= set(retrieval) or set(retrieval) - (
            allowed
            | {"PREFER_OFFICIAL_REFERENCES", "NONCOMMERCIAL_DATA_USE", "CONTEXTUAL_RETRIEVAL"}
        ):
            raise ValueError("Invalid verified retrieval release settings")
        config.update(retrieval)
    security = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    app_container = {
        "name": "app",
        "image": image,
        "ports": [{"containerPort": 8000}],
        "envFrom": [
            {"configMapRef": {"name": "silvercare-config"}},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "192Mi"},
            "limits": {"cpu": "1000m", "memory": "512Mi"},
        },
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        "readinessProbe": {
            "httpGet": {"path": "/health/live", "port": 8000},
            "periodSeconds": 10,
        },
        "livenessProbe": {
            "httpGet": {"path": "/health/live", "port": 8000},
            "initialDelaySeconds": 15,
            "periodSeconds": 20,
        },
    }
    if kdca_key and kdca_key.get_secret_value():
        app_container["env"] = [
            {
                "name": "KDCA_HEALTH_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {"name": "silvercare-health", "key": "KDCA_HEALTH_API_KEY"}
                },
            }
        ]
    caddyfile = (
        "{\n  http_port 8080\n  https_port 8443\n  admin off\n"
        "  servers {\n    max_header_size 32KB\n  }\n}\n"
        f"{host} {{\n"
        "  encode zstd gzip\n"
        "  request_body {\n    max_size 5MB\n  }\n"
        "  reverse_proxy silvercare:8000 {\n"
        "    header_up X-Real-IP {remote_host}\n"
        "    transport http {\n      response_header_timeout 120s\n    }\n"
        "  }\n}\n"
    )
    resources = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": NAMESPACE,
                "labels": {
                    "pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/enforce-version": "latest",
                },
            },
        },
        resource(
            "ServiceAccount",
            "silvercare",
            automountServiceAccountToken=False,
            **{
                "metadata": {
                    "name": "silvercare",
                    "namespace": NAMESPACE,
                    "annotations": {
                        "azure.workload.identity/client-id": client_id,
                        "azure.workload.identity/tenant-id": tenant,
                    },
                }
            },
        ),
        resource("ConfigMap", "silvercare-config", data=config),
        resource(
            "Deployment",
            "silvercare",
            spec={
                "replicas": 1,
                "revisionHistoryLimit": 3,
                "selector": {"matchLabels": {"app": "silvercare"}},
                "template": {
                    "metadata": {
                        "labels": {"app": "silvercare", "azure.workload.identity/use": "true"}
                    },
                    "spec": {
                        "serviceAccountName": "silvercare",
                        "securityContext": security,
                        "containers": [app_container],
                        "volumes": [{"name": "tmp", "emptyDir": {}}],
                        "terminationGracePeriodSeconds": 30,
                    },
                },
            },
        ),
        resource(
            "Service",
            "silvercare",
            spec={
                "selector": {"app": "silvercare"},
                "ports": [{"port": 8000, "targetPort": 8000}],
            },
        ),
        resource("ConfigMap", "silvercare-edge", data={"Caddyfile": caddyfile}),
        resource(
            "PersistentVolumeClaim",
            "silvercare-tls",
            spec={
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "managed-csi",
                "resources": {"requests": {"storage": "1Gi"}},
            },
        ),
        resource(
            "Deployment",
            "silvercare-edge",
            spec={
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "revisionHistoryLimit": 2,
                "selector": {"matchLabels": {"app": "silvercare-edge"}},
                "template": {
                    "metadata": {"labels": {"app": "silvercare-edge"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 10001,
                            "runAsGroup": 10001,
                            "fsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "caddy",
                                "image": "caddy:2.10.2-alpine",
                                "ports": [{"containerPort": 8080}, {"containerPort": 8443}],
                                "env": [
                                    {"name": "XDG_DATA_HOME", "value": "/data"},
                                    {"name": "XDG_CONFIG_HOME", "value": "/config"},
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "300m", "memory": "192Mi"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "caddyfile",
                                        "mountPath": "/etc/caddy",
                                        "readOnly": True,
                                    },
                                    {"name": "data", "mountPath": "/data"},
                                    {"name": "config", "mountPath": "/config"},
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                                "readinessProbe": {
                                    "tcpSocket": {"port": 8080},
                                    "periodSeconds": 10,
                                },
                            }
                        ],
                        "volumes": [
                            {"name": "caddyfile", "configMap": {"name": "silvercare-edge"}},
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": "silvercare-tls"},
                            },
                            {"name": "config", "emptyDir": {}},
                            {"name": "tmp", "emptyDir": {}},
                        ],
                    },
                },
            },
        ),
        resource(
            "Service",
            "silvercare-edge",
            **{
                "metadata": {
                    "name": "silvercare-edge",
                    "namespace": NAMESPACE,
                    "annotations": {
                        "service.beta.kubernetes.io/azure-dns-label-name": host.split(".")[0],
                    },
                }
            },
            spec={
                "type": "LoadBalancer",
                "externalTrafficPolicy": "Local",
                "selector": {"app": "silvercare-edge"},
                "ports": [
                    {"name": "http", "port": 80, "targetPort": 8080},
                    {"name": "https", "port": 443, "targetPort": 8443},
                ],
            },
        ),
        resource(
            "NetworkPolicy",
            "silvercare-app-ingress",
            spec={
                "podSelector": {"matchLabels": {"app": "silvercare"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": {"app": "silvercare-edge"}}}],
                        "ports": [{"protocol": "TCP", "port": 8000}],
                    }
                ],
            },
        ),
    ]
    if kdca_key and kdca_key.get_secret_value():
        resources.insert(
            1,
            resource(
                "Secret",
                "silvercare-health",
                type="Opaque",
                stringData={"KDCA_HEALTH_API_KEY": kdca_key.get_secret_value()},
            ),
        )
    return resources


def verify_public_url(url: str) -> None:
    with httpx.Client(timeout=15) as client:
        for attempt in range(24):
            try:
                response = client.get(f"{url}/health/live")
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
                reason = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                reason = type(exc).__name__
            if attempt < 23:
                print(f"Waiting for trusted public HTTPS ({reason}); retrying in 5s.", flush=True)
                time.sleep(5)
        raise RuntimeError("Public HTTPS health verification failed; deployment is not ready")


def deploy(skip_infra: bool, skip_build: bool, tag: str) -> None:
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    RUNTIME.chmod(0o700)
    if not skip_infra:
        print("Provisioning dedicated resources (Azure usage charges apply).", flush=True)
        command("az", "group", "create", "-n", GROUP, "-l", LOCATION, "-o", "none")
        command(
            "az",
            "deployment",
            "group",
            "create",
            "-g",
            GROUP,
            "-n",
            "silvercare-infra",
            "--template-file",
            str(ROOT / "infra/main.bicep"),
            "-o",
            "none",
        )
    outputs = az_json(
        "deployment",
        "group",
        "show",
        "-g",
        GROUP,
        "-n",
        "silvercare-infra",
        "--query",
        "properties.outputs",
    )
    output = {key: value["value"] for key, value in outputs.items()}
    retrieval_path = ROOT / "data/processed/expanded/release.json"
    retrieval = json.loads(retrieval_path.read_text()) if retrieval_path.exists() else None
    account = az_json("account", "show")
    foundry_id = az_json(
        "cognitiveservices",
        "account",
        "show",
        "-g",
        FOUNDRY_GROUP,
        "-n",
        FOUNDRY_NAME,
        "--query",
        "id",
    )
    principal = output["identityPrincipalId"]
    role(principal, "Cognitive Services OpenAI User", foundry_id)
    role(principal, "Cognitive Services Speech User", foundry_id)
    role(principal, "Search Index Data Reader", output["searchId"])
    role(output["kubeletPrincipalId"], "AcrPull", output["registryId"])
    user_id = az_json("ad", "signed-in-user", "show", "--query", "id")
    role(user_id, "Search Index Data Contributor", output["searchId"], "User")
    if retrieval is None:
        print("Preparing and indexing reviewed reference notes.", flush=True)
        asyncio.run(seed_reference(output["searchEndpoint"]))
    else:
        search_name = retrieval["AZURE_SEARCH_ENDPOINT"].split("://", 1)[1].split(".", 1)[0]
        active_search_id = az_json(
            "search", "service", "show", "-g", GROUP, "-n", search_name, "--query", "id"
        )
        role(principal, "Search Index Data Reader", active_search_id)
        print("Using the verified expanded retrieval release.", flush=True)
    host = f"ondam-{account['id'][:8]}.{LOCATION}.cloudapp.azure.com"
    image = f"{output['registryServer']}/silvercare:{tag}"
    if not skip_build:
        print(f"Building container image {tag}.", flush=True)
        command(
            "az",
            "acr",
            "build",
            "--registry",
            output["registryName"],
            "--image",
            f"silvercare:{tag}",
            "--file",
            str(ROOT / "Dockerfile"),
            "--no-logs",
            str(ROOT),
        )
    kubectl = shutil.which("kubectl") or str(RUNTIME / "bin/kubectl")
    kubeconfig = RUNTIME / "kubeconfig"
    command(
        "az",
        "aks",
        "get-credentials",
        "-g",
        GROUP,
        "-n",
        output["clusterName"],
        "--admin",
        "--file",
        str(kubeconfig),
        "--overwrite-existing",
    )
    kubeconfig.chmod(0o600)
    resources = kubernetes_resources(
        image,
        output["identityClientId"],
        account["tenantId"],
        host,
        output["searchEndpoint"],
        foundry_id,
        retrieval,
        Settings().kdca_health_api_key,
    )
    command(
        kubectl,
        "--kubeconfig",
        str(kubeconfig),
        "apply",
        "-f",
        "-",
        data=json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}),
    )
    state = {
        "url": f"https://{host}",
        "image": image,
        "resource_group": GROUP,
        "cluster": output["clusterName"],
        "search_endpoint": (
            retrieval["AZURE_SEARCH_ENDPOINT"] if retrieval else output["searchEndpoint"]
        ),
        "kubeconfig": str(kubeconfig),
    }
    print("Waiting for application and public edge readiness.", flush=True)
    for name in ("silvercare", "silvercare-edge"):
        command(
            kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{name}",
            "--timeout=600s",
        )
    verify_public_url(state["url"])
    command(
        kubectl,
        "--kubeconfig",
        str(kubeconfig),
        "-n",
        NAMESPACE,
        "delete",
        "secret",
        "silvercare-session",
        "--ignore-not-found",
    )
    state_path = RUNTIME / "deployment.json"
    temporary = state_path.with_suffix(".json.partial")
    try:
        with temporary.open("w") as stream:
            temporary.chmod(0o600)
            json.dump(state, stream, indent=2)
        temporary.replace(state_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    print(json.dumps(state, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision and deploy SilverCare on public HTTPS AKS"
    )
    parser.add_argument("--skip-infra", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--tag", default="v1")
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        deploy(args.skip_infra, args.skip_build, args.tag)
    except (RuntimeError, OSError, ValueError, KeyError, UpstreamError) as exc:
        parser.exit(1, f"Deployment failed: {exc}\n")


if __name__ == "__main__":
    main()
