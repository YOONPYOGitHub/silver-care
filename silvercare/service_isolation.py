"""Dedicated runtime bindings for the four services; shared ingress is unchanged."""

import argparse
import asyncio
import copy
import fcntl
import hashlib
import json
import re
from typing import Any

from silvercare.deploy import GROUP, LOCATION, RUNTIME, az_json, role
from silvercare.deploy_comparison import (
    MANAGED_API_VERSION,
    MANAGED_MODELS,
    apply,
    kubectl,
    managed_body,
    obj,
    reject_shared_deployment,
)

STATE = RUNTIME / "service-isolation-state.json"
API = "2026-11-01"
PROFILES = {
    "azure": {
        "number": 1,
        "namespace": "silvercare",
        "app": "silvercare",
        "pool": "azurecpu",
        "workload": "azure",
        "vm_size": "Standard_D2ds_v5",
    },
    "local-rag": {
        "number": 2,
        "namespace": "silvercare-local",
        "app": "local-rag",
        "pool": "localmem",
        "workload": "local-ai",
        "vm_size": "Standard_D8ds_v5",
    },
    "local-only": {
        "number": 3,
        "namespace": "silvercare-local",
        "app": "local-only",
        "pool": "onlycpu",
        "workload": "only",
        "vm_size": "Standard_D2ds_v5",
    },
    "qwen-azure-rag": {
        "number": 4,
        "namespace": "silvercare-local",
        "app": "qwen-azure-rag",
        "pool": "qwazcpu",
        "workload": "qwenazure",
        "vm_size": "Standard_D2ds_v5",
    },
}


def save(state: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".writing")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(STATE)


def load() -> dict[str, Any]:
    state: dict[str, Any] = json.loads(STATE.read_text())
    if not isinstance(state, dict):
        raise ValueError("Invalid isolation state")
    return state


def arm(
    method: str, resource: str, *, body: dict[str, Any] | None = None, version: str = API
) -> Any:
    args = [
        "rest",
        "--method",
        method,
        "--url",
        "https://management.azure.com" + resource + "?api-version=" + version,
    ]
    if body is not None:
        args += ["--body", json.dumps(body)]
    return az_json(*args)


def snapshot() -> dict[str, Any]:
    if STATE.exists():
        return load()
    reject_shared_deployment()
    account = az_json("account", "show")
    subscription, tenant = account["id"], account["tenantId"]
    issuer = az_json(
        "aks", "show", "-g", GROUP, "-n", "aks-silvercare", "--query", "oidcIssuerProfile.issuerUrl"
    )
    state: dict[str, Any] = {
        "format_version": 1,
        "subscription": subscription,
        "tenant": tenant,
        "issuer": issuer,
        "profiles": {},
        "activated": False,
        "shared_exceptions": [
            "HTTPS ingress/TLS/public IP",
            "AKS control plane/ACR",
            "external KDCA API and subscription/platform-wide limits",
        ],
    }
    for name, profile in PROFILES.items():
        ns, app = profile["namespace"], profile["app"]
        dep = json.loads(kubectl("-n", str(ns), "get", "deployment", str(app), "-o", "json"))
        config_name = "silvercare-config" if name == "azure" else str(app) + "-config"
        config = json.loads(kubectl("-n", str(ns), "get", "configmap", config_name, "-o", "json"))
        number = profile["number"]
        identity = az_json("identity", "show", "-g", GROUP, "-n", "silvercare-app")
        state["profiles"][name] = {
            **profile,
            "image": dep["spec"]["template"]["spec"]["containers"][0]["image"],
            "original_spec": dep["spec"],
            "original_config": config["data"],
            "config_name": config_name,
            "account_name": f"ondam-v{number}-{subscription[:8]}",
            "identity_name": f"silvercare-v{number}",
            "service_account": f"silvercare-v{number}",
            "original_identity": identity["clientId"],
        }
    gateway = json.loads(
        kubectl(
            "-n", "silvercare-local", "get", "configmap", "foundry-gateway-config", "-o", "json"
        )
    )["data"]
    state["original_gateway"] = gateway
    state["original_foundry_id"] = state["profiles"]["qwen-azure-rag"]["original_config"][
        "AZURE_SPEECH_RESOURCE_ID"
    ]
    for deployment in ("gpt-6.1-terra", "silvercare-embedding"):
        state[deployment] = arm("get", state["original_foundry_id"] + "/deployments/" + deployment)
    state["profiles"]["qwen-azure-rag"]["account_name"] = (
        gateway["FOUNDRY_ENDPOINT"].split("://", 1)[1].split(".", 1)[0]
    )
    save(state)
    return state


def provision_accounts() -> None:
    state = snapshot()
    base = f"/subscriptions/{state['subscription']}/resourceGroups/{GROUP}"
    existing = {
        row["name"]: row for row in az_json("cognitiveservices", "account", "list", "-g", GROUP)
    }
    for name in ("azure", "local-rag", "local-only"):
        profile = state["profiles"][name]
        resource = (
            base + "/providers/Microsoft.CognitiveServices/accounts/" + profile["account_name"]
        )
        if profile["account_name"] not in existing:
            row = arm(
                "put",
                resource,
                body={
                    "kind": "AIServices",
                    "location": LOCATION,
                    "sku": {"name": "S0"},
                    "identity": {"type": "SystemAssigned"},
                    "properties": {
                        "customSubDomainName": profile["account_name"],
                        "allowProjectManagement": True,
                        "disableLocalAuth": True,
                        "publicNetworkAccess": "Enabled",
                    },
                    "tags": {"project": "silver-care", "service-profile": name},
                },
            )
        else:
            row = existing[profile["account_name"]]
            if (
                row["kind"] != "AIServices"
                or row.get("tags", {}).get("project") != "silver-care"
                or row.get("tags", {}).get("service-profile") != name
            ):
                raise ValueError("Unexpected resource ownership; refusing to reuse this account")
        profile["account_id"] = resource
        profile["endpoint"] = "https://" + profile["account_name"] + ".services.ai.azure.com"
        profile["openai_endpoint"] = "https://" + profile["account_name"] + ".openai.azure.com"
        profile["speech_endpoint"] = (
            "https://" + profile["account_name"] + ".cognitiveservices.azure.com"
        )
        print(
            json.dumps(
                {
                    "profile": name,
                    "account": profile["account_name"],
                    "state": row["properties"]["provisioningState"],
                }
            ),
            flush=True,
        )
        save(state)
    fourth = state["profiles"]["qwen-azure-rag"]
    fourth["account_id"] = state["original_foundry_id"]
    fourth["endpoint"] = state["original_gateway"]["FOUNDRY_ENDPOINT"]
    fourth["openai_endpoint"] = fourth["original_config"]["AZURE_OPENAI_ENDPOINT"]
    fourth["speech_endpoint"] = fourth["original_config"]["AZURE_SPEECH_ENDPOINT"]
    save(state)


def provision_identity() -> None:
    state = load()
    existing = {row["name"]: row for row in az_json("identity", "list", "-g", GROUP)}
    for name, profile in state["profiles"].items():
        identity_name = profile["identity_name"]
        identity = existing.get(identity_name)
        if identity is None:
            identity = az_json("identity", "create", "-g", GROUP, "-n", identity_name)
        profile["client_id"], profile["principal_id"] = (
            identity["clientId"],
            identity["principalId"],
        )
        credentials = az_json(
            "identity",
            "federated-credential",
            "list",
            "-g",
            GROUP,
            "--identity-name",
            identity_name,
        )
        subject = f"system:serviceaccount:{profile['namespace']}:{profile['service_account']}"
        if not credentials:
            az_json(
                "identity",
                "federated-credential",
                "create",
                "-g",
                GROUP,
                "--identity-name",
                identity_name,
                "-n",
                "aks",
                "--issuer",
                state["issuer"],
                "--subject",
                subject,
                "--audiences",
                "api://AzureADTokenExchange",
            )
        elif not any(
            row["subject"] == subject and row["issuer"] == state["issuer"] for row in credentials
        ):
            raise ValueError("Unexpected federated identity; refusing credential replacement")
        roles = ["Cognitive Services Speech User"]
        if name in {"azure", "qwen-azure-rag"}:
            roles.append("Cognitive Services OpenAI User")
        if name != "azure":
            roles.append("Foundry User")
        for role_name in roles:
            role(profile["principal_id"], role_name, profile["account_id"])
        sa = obj("ServiceAccount", profile["service_account"])
        sa["metadata"]["namespace"] = profile["namespace"]
        sa["metadata"]["annotations"] = {
            "azure.workload.identity/client-id": profile["client_id"],
            "azure.workload.identity/tenant-id": state["tenant"],
        }
        apply([sa])
        save(state)
        print("Dedicated identity ready:", name, flush=True)


def provision_model(profile_name: str, model_name: str) -> None:
    state = load()
    profile = state["profiles"][profile_name]
    if profile_name == "qwen-azure-rag":
        raise ValueError("Version4's actively used deployments must not be modified")
    if model_name in {"gpt-6.1-terra", "silvercare-embedding"}:
        if profile_name != "azure":
            raise ValueError("Azure baseline models belong only to version1")
        original = state[model_name]
        resource = profile["account_id"] + "/deployments/" + model_name
        body = {
            "sku": original["sku"],
            "properties": {
                "model": original["properties"]["model"],
                "raiPolicyName": original["properties"]["raiPolicyName"],
                "versionUpgradeOption": original["properties"]["versionUpgradeOption"],
            },
        }
        existing = arm("get", profile["account_id"] + "/deployments").get("value", [])
        found = next((row for row in existing if row["name"] == model_name), None)
        if found is None:
            found = arm("put", resource, body=body)
    else:
        if model_name not in MANAGED_MODELS or (
            model_name == "ondam-qwen-embedding" and profile_name != "local-rag"
        ):
            raise ValueError("Unsupported dedicated managed model binding")
        model, template = MANAGED_MODELS[model_name]
        body = managed_body(model, template)
        resource = profile["account_id"] + "/managedComputeDeployments/" + model_name
        existing = arm(
            "get", profile["account_id"] + "/managedComputeDeployments", version=MANAGED_API_VERSION
        ).get("value", [])
        found = next((row for row in existing if row["name"] == model_name), None)
        if found is None:
            found = arm("put", resource, body=body, version=MANAGED_API_VERSION)
    if any(found["properties"].get(key) != value for key, value in body["properties"].items()):
        raise ValueError("Existing model/template/settings differ; refusing to replace it")
    if any(found["sku"].get(key) != value for key, value in body["sku"].items()):
        raise ValueError("Existing model capacity differs; refusing to replace it")
    profile.setdefault("models", {})[model_name] = resource
    save(state)
    print(
        json.dumps(
            {
                "profile": profile_name,
                "model": model_name,
                "state": found["properties"]["provisioningState"],
            }
        ),
        flush=True,
    )


def prepare_search() -> None:
    state = load()
    first, fourth = state["profiles"]["azure"], state["profiles"]["qwen-azure-rag"]
    search_name = "silvercare-v1-" + state["subscription"][:8]
    search = az_json("search", "service", "show", "-g", GROUP, "-n", search_name)
    first["search_id"] = search["id"]
    first["search_endpoint"] = f"https://{search_name}.search.windows.net"
    original_name = fourth["original_config"]["AZURE_SEARCH_ENDPOINT"].split("://")[1].split(".")[0]
    fourth["search_id"] = az_json("search", "service", "show", "-g", GROUP, "-n", original_name)[
        "id"
    ]
    fourth["search_endpoint"] = fourth["original_config"]["AZURE_SEARCH_ENDPOINT"]
    operator = az_json("ad", "signed-in-user", "show")["id"]
    for role_name in ("Search Service Contributor", "Search Index Data Contributor"):
        role(operator, role_name, first["search_id"], principal_type="User")
    for profile in (first, fourth):
        role(profile["principal_id"], "Search Index Data Reader", profile["search_id"])
    save(state)


async def clone_search() -> None:
    from silvercare.azure import SEARCH_API, AzureServices
    from silvercare.config import Settings
    from silvercare.data import SearchDocument
    from silvercare.expanded import (
        OUTPUT,
        EmbeddingCache,
        indexed_document_hashes,
        upload,
    )

    state = load()
    first = state["profiles"]["azure"]
    destination = first["search_endpoint"]
    if destination == state["profiles"]["qwen-azure-rag"]["search_endpoint"]:
        raise ValueError("Refusing to write to the actively used fourth Search service")
    for profile in state["profiles"].values():
        dep = json.loads(
            kubectl("-n", profile["namespace"], "get", "deployment", profile["app"], "-o", "json")
        )
        for source in dep["spec"]["template"]["spec"]["containers"][0].get("envFrom", []):
            if "configMapRef" in source:
                config = json.loads(
                    kubectl(
                        "-n",
                        profile["namespace"],
                        "get",
                        "configmap",
                        source["configMapRef"]["name"],
                        "-o",
                        "json",
                    )
                )["data"]
                if config.get("AZURE_SEARCH_ENDPOINT") == destination:
                    raise ValueError("Refusing to reconcile an index already used by a live app")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="development",
        azure_openai_endpoint=first["openai_endpoint"],
        azure_embedding_deployment="silvercare-embedding",
        embedding_dimensions=512,
        azure_search_endpoint=destination,
        azure_search_index="migration-not-active",
        azure_search_case_index="migration-not-active-cases",
        contextual_retrieval=True,
        noncommercial_data_use=True,
    )
    old_settings = settings.model_copy(
        update={"azure_search_endpoint": first["original_config"]["AZURE_SEARCH_ENDPOINT"]}
    )
    source_service, target_service = AzureServices(old_settings), AzureServices(settings)
    cache = EmbeddingCache(OUTPUT / "embeddings.sqlite3", "silvercare-embedding", 512)
    report: dict[str, Any] = {"destination": destination, "indexes": {}}
    try:
        for filename, field in (
            ("knowledge.jsonl", "AZURE_SEARCH_INDEX"),
            ("cases.jsonl", "AZURE_SEARCH_CASE_INDEX"),
        ):
            path = OUTPUT / filename
            documents = [
                SearchDocument.model_validate_json(line)
                for line in path.read_bytes().splitlines()
                if line.strip()
            ]
            if any(cache.get(doc.title + "\n" + doc.content) is None for doc in documents):
                raise ValueError("Exact cached vectors are required; no re-embedding is permitted")
            index = first["original_config"][field]
            expected = {
                doc.id: hashlib.sha256(
                    json.dumps(doc.model_dump(), sort_keys=True).encode()
                ).hexdigest()
                for doc in documents
            }
            if await indexed_document_hashes(source_service, index) != expected:
                raise ValueError("Prepared corpus differs from the original live index")
            original_schema = (
                await source_service.request(
                    "search",
                    "GET",
                    old_settings.azure_search_endpoint
                    + f"/indexes/{index}?api-version={SEARCH_API}",
                    headers={"Authorization": f"Bearer {await source_service.token(search=True)}"},
                )
            ).json()
            result = await upload(target_service, cache, index, path)
            target_schema = (
                await target_service.request(
                    "search",
                    "GET",
                    destination + f"/indexes/{index}?api-version={SEARCH_API}",
                    headers={"Authorization": f"Bearer {await target_service.token(search=True)}"},
                )
            ).json()
            if {k: v for k, v in original_schema.items() if not k.startswith("@odata.")} != {
                k: v for k, v in target_schema.items() if not k.startswith("@odata.")
            }:
                raise ValueError("Cloned Search schema differs from the original")
            if await indexed_document_hashes(target_service, index) != expected:
                raise ValueError("Cloned document content/IDs differ from the original")
            if result["new_embeddings"] != 0:
                raise ValueError("Unexpected re-embedding during exact corpus clone")
            report["indexes"][index] = result
            print(json.dumps({"index": index, **result}), flush=True)
        first["search_clone_verified"] = True
        save(state)
        (RUNTIME / "isolation-search-clone.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        cache.close()
        await source_service.close()
        await target_service.close()


def bindings(profile_name: str, profile: dict[str, Any]) -> dict[str, str]:
    config = dict(profile["original_config"])
    config.update(
        {
            "AZURE_SPEECH_ENDPOINT": profile["speech_endpoint"],
            "AZURE_SPEECH_RESOURCE_ID": profile["account_id"],
            "AZURE_OPENAI_ENDPOINT": profile["openai_endpoint"],
        }
    )
    if profile_name in {"azure", "qwen-azure-rag"}:
        config["AZURE_SEARCH_ENDPOINT"] = profile["search_endpoint"]
    else:
        config.update(
            {"AZURE_SEARCH_ENDPOINT": "", "AZURE_SEARCH_INDEX": "", "AZURE_SEARCH_CASE_INDEX": ""}
        )
    if profile_name != "azure":
        config["LOCAL_LLM_ENDPOINT"] = f"http://{profile['app']}-foundry:8080"
        if profile_name == "local-rag":
            config["LOCAL_EMBEDDING_ENDPOINT"] = config["LOCAL_LLM_ENDPOINT"]
    return config


def schedule(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodeSelector": {"workload": profile["workload"]},
        "tolerations": [
            {
                "key": "workload",
                "operator": "Equal",
                "value": profile["workload"],
                "effect": "NoSchedule",
            }
        ],
        "serviceAccountName": profile["service_account"],
    }


def gateway_resources(
    profile_name: str, profile: dict[str, Any], image: str
) -> list[dict[str, Any]]:
    if profile_name == "azure":
        raise ValueError("Version1 does not use a Qwen gateway")
    name = profile["app"] + "-foundry"
    labels = {"app": name, "service-profile": profile_name, "azure.workload.identity/use": "true"}
    cm = obj(
        "ConfigMap",
        name + "-config",
        data={
            "FOUNDRY_ENDPOINT": profile["endpoint"],
            "FOUNDRY_LLM_DEPLOYMENT": "ondam-qwen-4b",
            "FOUNDRY_EMBEDDING_DEPLOYMENT": (
                "ondam-qwen-embedding" if profile_name == "local-rag" else ""
            ),
        },
    )
    service = obj(
        "Service",
        name,
        spec={"selector": {"app": name}, "ports": [{"port": 8080, "targetPort": 8080}]},
    )
    deployment = obj(
        "Deployment",
        name,
        spec={
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    **schedule(profile),
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001},
                    "containers": [
                        {
                            "name": "gateway",
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
                            "envFrom": [{"configMapRef": {"name": name + "-config"}}],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "192Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi"},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "periodSeconds": 30,
                                "timeoutSeconds": 5,
                            },
                        }
                    ],
                },
            },
        },
    )
    return [cm, service, deployment]


def application_resources(
    profile_name: str,
    profile: dict[str, Any],
    *,
    canary: bool = False,
) -> list[dict[str, Any]]:
    name = profile["app"] + ("-isolation-canary" if canary else "")
    config_name = profile["app"] + "-isolated-config"
    labels = {"app": name, "service-profile": profile_name, "azure.workload.identity/use": "true"}
    spec = copy.deepcopy(profile["original_spec"])
    spec.update({"replicas": 1, "selector": {"matchLabels": {"app": name}}})
    spec["template"]["metadata"] = {"labels": labels}
    pod = spec["template"]["spec"]
    pod.update(schedule(profile))
    for container in pod["containers"]:
        container["image"] = profile["image"]
        for source in container.get("envFrom", []):
            if source.get("configMapRef", {}).get("name") == profile["config_name"]:
                source["configMapRef"]["name"] = config_name
    if canary:
        for container in pod["containers"]:
            for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
                container.pop(probe, None)
    config = bindings(profile_name, profile)
    result = [
        obj("ConfigMap", config_name, data=config),
        obj("Deployment", name, spec=spec),
    ]
    for resource in result:
        resource["metadata"]["namespace"] = profile["namespace"]
    return result


def deploy_gateways(image: str) -> None:
    state = load()
    if any(profile.get("cutover_complete") for profile in state["profiles"].values()):
        raise ValueError("Bulk gateway bootstrap is closed; update one profile with deploy-image")
    for name in ("local-rag", "local-only", "qwen-azure-rag"):
        profile = state["profiles"][name]
        apply(gateway_resources(name, profile, image))
        profile["gateway_image"] = image
    save(state)


PROBE = r"""
import json, os, time
import httpx
from azure.identity import DefaultAzureCredential

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=600) as client:
    client.get(READY_PATH).raise_for_status()
    started = time.monotonic()
    answer = client.post("/api/chat", json={
        "message": "건강하게 지내려면 오늘 무엇부터 해보면 좋을까요?", "history": []
    })
    answer.raise_for_status()
    body = answer.json()
    assert body["answer"].strip()
    report = {"chat_status": answer.status_code, "chat_seconds": round(time.monotonic()-started, 3),
              "sources": len(body["sources"]), "mode": body["mode"]}
    mp3 = client.post("/api/speech/synthesize", json={"text": "안녕하세요. 온담입니다."})
    mp3.raise_for_status()
    assert mp3.headers["content-type"].startswith("audio/mpeg") and len(mp3.content) > 500
    with DefaultAzureCredential() as credential:
        token = credential.get_token("https://cognitiveservices.azure.com/.default").token
    bearer = "aad#" + os.environ["AZURE_SPEECH_RESOURCE_ID"] + "#" + token
    wave = client.post(
        os.environ["AZURE_SPEECH_ENDPOINT"].rstrip("/") + "/tts/cognitiveservices/v1",
        headers={"Authorization": "Bearer " + bearer, "Content-Type": "application/ssml+xml",
                 "X-Microsoft-OutputFormat": "riff-16khz-16bit-mono-pcm"},
        content=('<speak version="1.0" xml:lang="ko-KR"><voice name="ko-KR-SunHiNeural">'
                 '안녕하세요. 온담입니다.</voice></speak>').encode(),
    )
    wave.raise_for_status()
    assert wave.content.startswith(b"RIFF")
    transcript = client.post("/api/speech/transcribe",
                             files={"audio": ("probe.wav", wave.content, "audio/wav")})
    transcript.raise_for_status()
    assert "안녕하세요" in transcript.json()["text"]
    report.update({"tts_bytes": len(mp3.content), "stt_text": transcript.json()["text"]})
    print(json.dumps(report, ensure_ascii=False))
"""


def verify_canary(profile_name: str) -> None:
    state = load()
    profile = state["profiles"][profile_name]
    readiness = profile["original_spec"]["template"]["spec"]["containers"][0]["readinessProbe"][
        "httpGet"
    ]["path"]
    code = "READY_PATH = " + json.dumps(readiness) + "\n" + PROBE
    output = kubectl(
        "-n",
        profile["namespace"],
        "exec",
        "deploy/" + profile["app"] + "-isolation-canary",
        "--",
        "/app/.venv/bin/python",
        "-c",
        code,
    )
    report = json.loads(output)
    profile["canary_verified"] = report
    save(state)
    print(json.dumps({"profile": profile_name, **report}, ensure_ascii=False), flush=True)


def validate_bindings(state: dict[str, Any]) -> None:
    profiles = state["profiles"]
    if set(profiles) != set(PROFILES):
        raise ValueError("All four service bindings are required")
    for field in ("pool", "account_id", "client_id", "speech_endpoint", "service_account"):
        if len({profile[field] for profile in profiles.values()}) != 4:
            raise ValueError("Cross-service sharing is prohibited: " + field)
    if profiles["azure"]["search_endpoint"] == profiles["qwen-azure-rag"]["search_endpoint"]:
        raise ValueError("The two Azure RAG services must have separate Search services")
    if profiles["qwen-azure-rag"]["account_id"] != state["original_foundry_id"]:
        raise ValueError("Version4's original model account must be preserved")


def activate_profile(profile_name: str) -> None:
    state = load()
    validate_bindings(state)
    profile = state["profiles"][profile_name]
    if profile.get("cutover_complete"):
        raise ValueError("This profile is already migrated; use deploy-image for later releases")
    if not profile.get("canary_verified"):
        raise ValueError("A successful text/STT/TTS canary is required before cutover")
    if profile_name == "azure" and not profile.get("search_clone_verified"):
        raise ValueError("The exact Search clone must be verified before baseline cutover")
    marker = obj("ConfigMap", "silvercare-runtime-isolation", data={"mode": "migration"})
    marker["metadata"]["namespace"] = "silvercare"
    apply([marker, *application_resources(profile_name, profile)])
    print(
        kubectl(
            "-n",
            profile["namespace"],
            "rollout",
            "status",
            "deployment/" + profile["app"],
            "--timeout=300s",
        ),
        flush=True,
    )
    profile["cutover_complete"] = True
    save(state)


def network_policy(profile_name: str, profile: dict[str, Any]) -> dict[str, Any]:
    same = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": profile["namespace"]}},
        "podSelector": {"matchLabels": {"service-profile": profile_name}},
    }
    edge = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "silvercare"}},
        "podSelector": {"matchLabels": {"app": "silvercare-edge"}},
    }
    dns = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": profile["app"] + "-boundary", "namespace": profile["namespace"]},
        "spec": {
            "podSelector": {"matchLabels": {"service-profile": profile_name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [same]}, {"from": [edge], "ports": [{"port": 8000}]}],
            "egress": [
                {"to": [same]},
                {
                    "to": [dns],
                    "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
                },
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": [
                                    "10.0.0.0/8",
                                    "172.16.0.0/12",
                                    "192.168.0.0/16",
                                    "169.254.0.0/16",
                                ],
                            }
                        }
                    ],
                    "ports": [{"port": 443}, {"port": 80}],
                },
            ],
        },
    }


def finalize() -> None:
    state = load()
    validate_bindings(state)
    if state["activated"]:
        raise ValueError("Migration is already finalized; no backing services were restarted")
    if not all(p.get("cutover_complete") for p in state["profiles"].values()):
        raise ValueError("Every profile must complete its cutover before enforcing boundaries")
    for name in ("local-search", "local-reranker"):
        patch = {"spec": {"template": {"metadata": {"labels": {"service-profile": "local-rag"}}}}}
        kubectl(
            "-n",
            "silvercare-local",
            "patch",
            "deployment",
            name,
            "--type=merge",
            "-p",
            json.dumps(patch),
        )
        print(
            kubectl(
                "-n",
                "silvercare-local",
                "rollout",
                "status",
                "deployment/" + name,
                "--timeout=300s",
            ),
            flush=True,
        )
    apply([network_policy(name, profile) for name, profile in state["profiles"].items()])
    kubectl("-n", "silvercare-local", "scale", "deployment/foundry-gateway", "--replicas=0")
    state["activated"] = True
    marker = obj("ConfigMap", "silvercare-runtime-isolation", data={"mode": "dedicated"})
    marker["metadata"]["namespace"] = "silvercare"
    apply([marker])
    save(state)


def deploy_image(profile_name: str, image: str, component: str) -> None:
    if not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", image):
        raise ValueError("An immutable image digest is required, not a mutable tag")
    state = load()
    profile = state["profiles"][profile_name]
    validate_bindings(state)
    if not profile.get("cutover_complete"):
        raise ValueError("Use the verified migration before updating isolated images")
    if component == "app":
        name = profile["app"]
    elif component == "gateway" and profile_name != "azure":
        name = profile["app"] + "-foundry"
    elif component in {"local-reranker", "local-search"} and profile_name == "local-rag":
        name = component
    else:
        raise ValueError("The selected component does not belong to this profile")
    dep = json.loads(kubectl("-n", profile["namespace"], "get", "deployment", name, "-o", "json"))
    pod = dep["spec"]["template"]["spec"]
    if pod["nodeSelector"] != {"workload": profile["workload"]}:
        raise ValueError("Node bindings drifted; refusing image update")
    if component in {"app", "gateway"} and pod["serviceAccountName"] != profile["service_account"]:
        raise ValueError("Identity bindings drifted; refusing image update")
    for source in pod["containers"][0].get("envFrom", []):
        if "configMapRef" in source:
            config = json.loads(
                kubectl(
                    "-n",
                    profile["namespace"],
                    "get",
                    "configmap",
                    source["configMapRef"]["name"],
                    "-o",
                    "json",
                )
            )["data"]
            expected = (
                bindings(profile_name, profile)
                if component == "app"
                else gateway_resources(profile_name, profile, image)[0]["data"]
            )
            if any(config.get(key) != value for key, value in expected.items()):
                raise ValueError("Resource bindings drifted; refusing image update")
    container_name = pod["containers"][0]["name"]
    kubectl(
        "-n",
        profile["namespace"],
        "set",
        "image",
        "deployment/" + name,
        container_name + "=" + image,
    )
    print(
        kubectl(
            "-n", profile["namespace"], "rollout", "status", "deployment/" + name, "--timeout=300s"
        ),
        flush=True,
    )
    profile["image" if component == "app" else component + "_image"] = image
    save(state)


def status() -> None:
    state = load()
    for name, profile in state["profiles"].items():
        kind = "deployments" if name == "azure" else "managedComputeDeployments"
        version = API if name == "azure" else MANAGED_API_VERSION
        models = arm("get", profile["account_id"] + "/" + kind, version=version)["value"]
        row = {
            "profile": name,
            "pool": profile["pool"],
            "account": profile["account_name"],
            "identity": profile["service_account"],
            "speech": profile["speech_endpoint"],
            "search": profile.get("search_endpoint"),
            "models": [
                {
                    "name": model["name"],
                    "state": model["properties"]["provisioningState"],
                    "model": model["properties"]["model"],
                }
                for model in models
            ],
        }
        if name == "qwen-azure-rag":
            deployment = profile["original_config"]["AZURE_EMBEDDING_DEPLOYMENT"]
            embedding = arm("get", profile["account_id"] + "/deployments/" + deployment)
            row["azure_embedding"] = {
                "name": deployment,
                "state": embedding["properties"]["provisioningState"],
                "model": embedding["properties"]["model"],
                "dimensions": 512,
            }
        print(json.dumps(row), flush=True)


def dispatch() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "snapshot",
            "accounts",
            "identities",
            "model",
            "search-roles",
            "clone-search",
            "gateways",
            "canary",
            "verify",
            "activate",
            "finalize",
            "deploy-image",
            "status",
        ],
    )
    parser.add_argument("--profile", choices=list(PROFILES))
    parser.add_argument("--model")
    parser.add_argument("--image")
    parser.add_argument(
        "--component", default="app", choices=["app", "gateway", "local-reranker", "local-search"]
    )
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot()
    elif args.command == "accounts":
        provision_accounts()
    elif args.command == "identities":
        provision_identity()
    elif args.command == "model":
        if not args.profile or not args.model:
            parser.error("A profile and model are required")
        provision_model(args.profile, args.model)
    elif args.command == "search-roles":
        prepare_search()
    elif args.command == "clone-search":
        asyncio.run(clone_search())
    elif args.command == "gateways":
        if not args.image:
            parser.error("A pinned gateway image is required")
        deploy_gateways(args.image)
    elif args.command == "canary":
        if not args.profile:
            parser.error("A profile is required")
        state = load()
        if state["profiles"][args.profile].get("cutover_complete"):
            parser.error("This profile is already migrated; bootstrap cannot overwrite live config")
        apply(application_resources(args.profile, state["profiles"][args.profile], canary=True))
    elif args.command in {"verify", "activate", "deploy-image"}:
        if not args.profile:
            parser.error("A profile is required")
        if args.command == "verify":
            verify_canary(args.profile)
        elif args.command == "activate":
            activate_profile(args.profile)
        else:
            if not args.image:
                parser.error("A pinned image is required")
            deploy_image(args.profile, args.image, args.component)
    elif args.command == "finalize":
        finalize()
    else:
        status()


def main() -> None:
    import os

    os.environ.setdefault("KUBECTL_REMOTE_COMMAND_WEBSOCKETS", "false")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        dispatch()


if __name__ == "__main__":
    main()
