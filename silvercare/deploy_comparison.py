"""Independent comparison apps and OSS search; the Azure baseline image is never changed."""

import argparse
import json
import subprocess
from typing import Any

from silvercare.deploy import RUNTIME, az_json

NAMESPACE = "silvercare-local"
BASELINE_NAMESPACE = "silvercare"
ML_RUNTIME = (
    "silvercare2a5aalbg7ugfc.azurecr.io/local-embedding-runtime@sha256:"
    "2538ea1c9640d3763b15af668039d24172d063b42337b0c27796fc2be180c78d"
)
MANAGED_API_VERSION = "2026-03-15-preview"
MANAGED_MODELS = {
    "ondam-qwen-4b": (
        "qwen--qwen3.5-4b/versions/3",
        "qwen--qwen3-5-4b--256k-nvidia-a100",
    ),
    "ondam-qwen-embedding": (
        "qwen--qwen3-embedding-0.6b/versions/2",
        "qwen--qwen3-embedding-06b--a100",
    ),
}


def managed_body(model: str, template: str) -> dict[str, Any]:
    registry = "azureml://registries/azure-huggingface"
    return {
        "sku": {"name": "GlobalManagedCompute", "capacity": 1},
        "properties": {
            "model": f"{registry}/models/{model}",
            "deploymentTemplate": f"{registry}/deploymenttemplates/{template}/labels/latest",
            "acceleratorType": "A100_80GB",
        },
    }


def manage_models(create: bool = False) -> None:
    subscription = az_json("account", "show")["id"]
    url = (
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/RG-AI"
        "/providers/Microsoft.CognitiveServices/accounts/ai-foundry-m098047"
        "/managedComputeDeployments"
    )
    existing = {
        row["name"]: row
        for row in az_json(
            "rest", "--method", "get", "--url", f"{url}?api-version={MANAGED_API_VERSION}"
        )["value"]
    }
    for name, (model, template) in MANAGED_MODELS.items():
        expected = managed_body(model, template)
        row = existing.get(name)
        if row is None and create:
            row = az_json(
                "rest",
                "--method",
                "put",
                "--url",
                f"{url}/{name}?api-version={MANAGED_API_VERSION}",
                "--body",
                json.dumps(expected),
            )
        if row is not None:
            for key, value in expected["properties"].items():
                if row["properties"].get(key) != value:
                    raise ValueError(
                        f"Existing deployment {name} has unexpected {key}; not changed"
                    )
        print(
            json.dumps(
                {
                    "deployment": name,
                    "state": row["properties"]["provisioningState"] if row else "NotCreated",
                    "model": model,
                    "accelerator": "A100_80GB",
                    "billing": "hourly managed compute, not serverless token billing",
                }
            ),
            flush=True,
        )


def kubectl(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        [str(RUNTIME / "bin/kubectl"), "--kubeconfig", str(RUNTIME / "kubeconfig"), *args],
        input=input_text,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (
            "stderr withheld because stdin may contain secrets" if input_text else result.stderr
        )
        raise RuntimeError(f"Comparison kubectl failed ({result.returncode}): {detail}")
    return result.stdout


def obj(kind: str, name: str, **body: Any) -> dict[str, Any]:
    versions = {"Deployment": "apps/v1", "Job": "batch/v1"}
    return {
        "apiVersion": versions.get(kind, "v1"),
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE},
        **body,
    }


def scheduling(uid: int = 10001) -> dict[str, Any]:
    return {
        "nodeSelector": {"workload": "local-ai"},
        "tolerations": [
            {"key": "workload", "operator": "Equal", "value": "local-ai", "effect": "NoSchedule"}
        ],
        "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "fsGroup": uid},
        "automountServiceAccountToken": False,
    }


def deployment(
    name: str,
    container: dict[str, Any],
    *,
    volumes: list[dict[str, Any]] | None = None,
    identity: bool = False,
    uid: int = 10001,
) -> dict[str, Any]:
    pod = scheduling(uid)
    if identity:
        pod["serviceAccountName"] = "silvercare"
        pod["terminationGracePeriodSeconds"] = 660
    pod["containers"] = [{"name": name, **container}]
    if volumes:
        pod["volumes"] = volumes
    labels = {"app": name}
    if identity:
        labels["azure.workload.identity/use"] = "true"
    return obj(
        "Deployment",
        name,
        spec={
            "replicas": 1,
            "revisionHistoryLimit": 2,
            **(
                {"strategy": {"type": "Recreate"}}
                if name in {"local-search", "local-reranker"}
                else {}
            ),
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    )


def service(name: str, port: int) -> dict[str, Any]:
    return obj(
        "Service",
        name,
        spec={
            "selector": {"app": name},
            "ports": [{"port": port, "targetPort": port}],
        },
    )


def resources(
    image: str,
    origin: str,
    client_id: str,
    tenant: str,
    speech: dict[str, str],
    foundry_endpoint: str,
) -> list[dict[str, Any]]:
    result = [obj("ServiceAccount", "silvercare")]
    result[0]["metadata"]["annotations"] = {
        "azure.workload.identity/client-id": client_id,
        "azure.workload.identity/tenant-id": tenant,
    }
    for name in ("local-search-data", "local-reranker-cache"):
        result.append(
            obj(
                "PersistentVolumeClaim",
                name,
                spec={
                    "accessModes": ["ReadWriteOnce"],
                    "storageClassName": "managed-csi",
                    "resources": {"requests": {"storage": "8Gi"}},
                },
            )
        )
    result.append(
        obj(
            "ConfigMap",
            "foundry-gateway-config",
            data={
                "FOUNDRY_ENDPOINT": foundry_endpoint,
                "FOUNDRY_LLM_DEPLOYMENT": "ondam-qwen-4b",
                "FOUNDRY_EMBEDDING_DEPLOYMENT": "ondam-qwen-embedding",
                "FOUNDRY_TOKEN_SCOPE": "https://cognitiveservices.azure.com/.default",
            },
        )
    )
    result.extend(
        [
            service("foundry-gateway", 8080),
            deployment(
                "foundry-gateway",
                {
                    "image": image,
                    "command": [
                        "/app/.venv/bin/uvicorn",
                        "silvercare.foundry_gateway:create_gateway",
                        "--factory",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "8080",
                        "--no-access-log",
                    ],
                    "envFrom": [{"configMapRef": {"name": "foundry-gateway-config"}}],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "192Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/health", "port": 8080},
                        "periodSeconds": 10,
                    },
                },
                identity=True,
            ),
        ]
    )
    result.extend(
        [
            service("local-search", 9200),
            deployment(
                "local-search",
                {
                    "image": "opensearchproject/opensearch:3.3.2",
                    "env": [
                        {"name": "discovery.type", "value": "single-node"},
                        {"name": "DISABLE_SECURITY_PLUGIN", "value": "true"},
                        {"name": "DISABLE_INSTALL_DEMO_CONFIG", "value": "true"},
                        {"name": "OPENSEARCH_JAVA_OPTS", "value": "-Xms1g -Xmx1g"},
                        {"name": "node.store.allow_mmap", "value": "false"},
                    ],
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "2Gi"},
                        "limits": {"cpu": "2", "memory": "4Gi"},
                    },
                    "volumeMounts": [{"name": "search", "mountPath": "/usr/share/opensearch/data"}],
                    "startupProbe": {
                        "httpGet": {"path": "/", "port": 9200},
                        "periodSeconds": 10,
                        "failureThreshold": 60,
                    },
                    "readinessProbe": {"httpGet": {"path": "/", "port": 9200}, "periodSeconds": 10},
                },
                uid=1000,
                volumes=[
                    {
                        "name": "search",
                        "persistentVolumeClaim": {"claimName": "local-search-data"},
                    }
                ],
            ),
        ]
    )
    # This legacy catalog model has no matching Foundry managed-compute template.
    # Only the reranker remains in AKS; its upstream revision and runtime digest are pinned.
    result.extend(
        [
            service("local-reranker", 8080),
            deployment(
                "local-reranker",
                {
                    "image": ML_RUNTIME,
                    "args": [
                        "--model-id",
                        "woxpas-ai/bge-reranker-v2-m3-onnx",
                        "--revision",
                        "c44ebc43de724ae8816668bb44d2e728e17faa18",
                        "--hostname",
                        "0.0.0.0",
                        "--port",
                        "8080",
                        "--dtype",
                        "float32",
                        "--max-batch-tokens",
                        "8192",
                        "--auto-truncate",
                        "false",
                        "--max-client-batch-size",
                        "64",
                        "--max-concurrent-requests",
                        "256",
                        "--huggingface-hub-cache",
                        "/models",
                    ],
                    "env": [
                        {"name": "OMP_NUM_THREADS", "value": "4"},
                        {"name": "RAYON_NUM_THREADS", "value": "4"},
                    ],
                    "resources": {
                        "requests": {"cpu": "2", "memory": "16Gi"},
                        "limits": {"cpu": "6", "memory": "24Gi"},
                    },
                    "volumeMounts": [{"name": "models", "mountPath": "/models"}],
                    "startupProbe": {
                        "httpGet": {"path": "/info", "port": 8080},
                        "periodSeconds": 10,
                        "failureThreshold": 120,
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/info", "port": 8080},
                        "timeoutSeconds": 5,
                        "periodSeconds": 10,
                    },
                },
                volumes=[
                    {
                        "name": "models",
                        "persistentVolumeClaim": {"claimName": "local-reranker-cache"},
                    }
                ],
            ),
        ]
    )
    for variant in ("local-rag", "local-only"):
        config = {
            "APP_ENV": "production",
            "PUBLIC_ORIGIN": origin,
            "TRUSTED_PROXY": "true",
            "SERVICE_VARIANT": variant,
            "BASE_PATH": "/" + variant,
            "LOCAL_LLM_ENDPOINT": "http://foundry-gateway:8080",
            "LOCAL_LLM_MODEL": "qwen3.5-4b",
            "LOCAL_REQUEST_TIMEOUT": "600",
            "RATE_LIMIT_PER_MINUTE": "24",
            "MAX_CONCURRENT_REQUESTS": "2",
            **speech,
        }
        if variant == "local-rag":
            config.update(
                {
                    "LOCAL_SEARCH_ENDPOINT": "http://local-search:9200",
                    "LOCAL_SEARCH_INDEX": "local-knowledge-v1",
                    "LOCAL_SEARCH_CASE_INDEX": "local-cases-v1",
                    "LOCAL_EMBEDDING_ENDPOINT": "http://foundry-gateway:8080",
                    "LOCAL_RERANKER_ENDPOINT": "http://local-reranker:8080",
                    "RETRIEVAL_MODE": "hybrid",
                    "EMBEDDING_DIMENSIONS": "1024",
                    "REWRITE_QUERIES": "true",
                    "SEMANTIC_RANKING": "true",
                    "PREFER_OFFICIAL_REFERENCES": "true",
                    "CONTEXTUAL_RETRIEVAL": "true",
                    "NONCOMMERCIAL_DATA_USE": "true",
                }
            )
        result.append(obj("ConfigMap", variant + "-config", data=config))
        container: dict[str, Any] = {
            "image": image,
            "envFrom": [{"configMapRef": {"name": variant + "-config"}}],
            "resources": {
                "requests": {"cpu": "100m", "memory": "192Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
            "readinessProbe": {
                "httpGet": {"path": "/health/ready", "port": 8000},
                "periodSeconds": 15,
                "timeoutSeconds": 10,
            },
            "livenessProbe": {
                "httpGet": {"path": "/health/live", "port": 8000},
                "periodSeconds": 20,
            },
        }
        if variant == "local-rag":
            container["env"] = [
                {
                    "name": "KDCA_HEALTH_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {"name": "silvercare-health", "key": "KDCA_HEALTH_API_KEY"}
                    },
                }
            ]
        result.extend([service(variant, 8000), deployment(variant, container, identity=True)])
    return result


def apply(items: list[dict[str, Any]]) -> None:
    print(
        kubectl(
            "apply",
            "-f",
            "-",
            input_text=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": items,
                }
            ),
        ),
        end="",
    )


def configure_identity() -> tuple[str, str]:
    identity = az_json("identity", "show", "-g", "rg-silver-care", "-n", "silvercare-app")
    issuer = az_json(
        "aks",
        "show",
        "-g",
        "rg-silver-care",
        "-n",
        "aks-silvercare",
        "--query",
        "oidcIssuerProfile.issuerUrl",
    )
    existing = az_json(
        "identity",
        "federated-credential",
        "list",
        "-g",
        "rg-silver-care",
        "--identity-name",
        "silvercare-app",
    )
    if not any(row["name"] == "silvercare-local" for row in existing):
        az_json(
            "identity",
            "federated-credential",
            "create",
            "-g",
            "rg-silver-care",
            "--identity-name",
            "silvercare-app",
            "-n",
            "silvercare-local",
            "--issuer",
            issuer,
            "--subject",
            f"system:serviceaccount:{NAMESPACE}:silvercare",
            "--audiences",
            "api://AzureADTokenExchange",
        )
    account = az_json("account", "show")
    secret = json.loads(
        kubectl("-n", BASELINE_NAMESPACE, "get", "secret", "silvercare-health", "-o", "json")
    )
    apply([obj("Secret", "silvercare-health", type="Opaque", data=secret["data"])])
    return identity["clientId"], account["tenantId"]


def route_config(original: str) -> str:
    if "/local-rag/" in original:
        raise ValueError("Comparison routes already exist; review changes explicitly")
    marker = "  reverse_proxy silvercare:8000 {"
    if original.count(marker) != 1 or not original.endswith("  }\n}\n"):
        raise ValueError("Unexpected baseline edge configuration; refusing automatic replacement")
    prefix, proxy = original.split(marker)
    routes = ""
    for variant in ("local-rag", "local-only"):
        routes += (
            f"  redir /{variant} /{variant}/ 308\n"
            f"  handle_path /{variant}/* {{\n"
            f"    reverse_proxy {variant}.{NAMESPACE}.svc.cluster.local:8000 {{\n"
            "      header_up X-Real-IP {remote_host}\n"
            "      transport http {\n        response_header_timeout 660s\n      }\n"
            "    }\n  }\n"
        )
    return prefix + routes + "  handle {\n" + marker + proxy[:-2] + "  }\n}\n"


def add_routes() -> None:
    baseline = json.loads(
        kubectl("-n", BASELINE_NAMESPACE, "get", "configmap", "silvercare-edge", "-o", "json")
    )
    updated = route_config(baseline["data"]["Caddyfile"])
    kubectl(
        "-n",
        BASELINE_NAMESPACE,
        "exec",
        "-i",
        "deployment/silvercare-edge",
        "--",
        "caddy",
        "validate",
        "--adapter",
        "caddyfile",
        "--config",
        "-",
        input_text=updated,
    )
    backup = RUNTIME / "comparison-baseline-edge.json"
    if not backup.exists():
        backup.write_text(json.dumps({"data": baseline["data"]}, indent=2))
        backup.chmod(0o600)
    print(
        kubectl(
            "-n",
            BASELINE_NAMESPACE,
            "patch",
            "configmap",
            "silvercare-edge",
            "--type=merge",
            "-p",
            json.dumps({"data": {"Caddyfile": updated}}),
        ),
        end="",
    )
    print(
        kubectl("-n", BASELINE_NAMESPACE, "rollout", "restart", "deployment/silvercare-edge"),
        end="",
    )
    print(
        kubectl(
            "-n",
            BASELINE_NAMESPACE,
            "rollout",
            "status",
            "deployment/silvercare-edge",
            "--timeout=300s",
        ),
        end="",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage isolated model-comparison services")
    parser.add_argument("command", choices=["deploy", "routes", "models", "model-status"])
    parser.add_argument("--image")
    parser.add_argument("--foundry-endpoint")
    args = parser.parse_args()
    try:
        if args.command == "deploy":
            if not args.image or not args.foundry_endpoint:
                raise ValueError("Image and Foundry endpoint are required")
            apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}])
            client_id, tenant = configure_identity()
            old = json.loads(
                kubectl(
                    "-n", BASELINE_NAMESPACE, "get", "configmap", "silvercare-config", "-o", "json"
                )
            )["data"]
            speech = {key: value for key, value in old.items() if key.startswith("AZURE_SPEECH_")}
            apply(
                resources(
                    args.image,
                    old["PUBLIC_ORIGIN"],
                    client_id,
                    tenant,
                    speech,
                    args.foundry_endpoint,
                )
            )
        elif args.command == "routes":
            add_routes()
        else:
            manage_models(create=args.command == "models")
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"Comparison operation failed: {error}\n")


if __name__ == "__main__":
    main()
