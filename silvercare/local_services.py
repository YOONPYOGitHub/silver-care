"""Shared Foundry generation with Azure Search, OSS RAG or model-only profiles."""

import hashlib
import json
import logging
import math
import re
from typing import Any

import httpx

from silvercare.azure import SEARCH_API, SYSTEM_PROMPT, AzureServices, UpstreamError
from silvercare.config import Settings
from silvercare.models import ChatRequest, ChatResponse
from silvercare.privacy import redact
from silvercare.safety import NOTICE, answer_display_notice, emergency_response, medical_caution
from silvercare.tracing import EvaluationTrace

logger = logging.getLogger(__name__)
MODEL_ONLY_NOTICE = "문서 검색이나 공식 API로 확인하지 않은 모델의 학습 지식 기반 답변입니다."
LLM_ONLY_PROMPT = (
    SYSTEM_PROMPT.split("자료와 사실:", 1)[0].replace(
        "제공된 근거가 없는 수치·조건·사실을 덧붙여 분량을 늘리지 마세요.",
        "불확실한 수치·조건·사실을 만들어 분량을 늘리지 마세요.",
    )
    + """자료와 사실:
문서 검색, 인터넷 검색, 공식 API 조회 없이 학습한 지식으로 답하는 비교용 도우미입니다.
알고 있는 일반 지식으로 충분히 답하고, 불확실하거나 최신 확인이 필요한 부분은 밝히세요.
근거 문서가 제공되지 않았다는 이유만으로 일반적인 설명까지 거절하지 마세요.
검색이나 원문 확인을 했다고 주장하거나 출처 번호, 검증하지 않은 링크를 만들지 마세요.
개인별 진단, 약의 용량 변경·중단, 치료·검사 시행이나 지원금·자격을 확정하지 마세요.
제품·성분·제형·투여 경로·국가·연령·의식·삼킴 상태에 따른 조건과 예외를 혼동하지 마세요.
응급처치·자가처치의 세부 동작이나 수치를 확신할 수 없으면 추정하지 말고 도움 요청을 안내하세요.

안전과 개인정보:
"""
    + SYSTEM_PROMPT.split("안전과 개인정보:", 1)[1]
)


class QwenServices(AzureServices):
    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        if settings.service_variant == "azure":
            raise ValueError("QwenServices requires an explicit comparison variant")
        if "client" not in kwargs:
            kwargs["client"] = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.local_request_timeout, connect=10)
            )
        super().__init__(settings, **kwargs)

    @property
    def generation_model(self) -> str:
        return self.settings.local_llm_model

    async def generate(
        self, instructions: str, inputs: str | list[dict[str, str]], max_tokens: int
    ) -> dict[str, Any]:
        messages = [{"role": "system", "content": instructions}]
        messages.extend(
            [{"role": "user", "content": inputs}] if isinstance(inputs, str) else inputs
        )
        _, separator, context_json = instructions.partition("\n검색 데이터(JSON):\n")
        if separator and json.loads(context_json).get("reference"):
            messages[-1] = {
                **messages[-1],
                "content": messages[-1]["content"]
                + "\n\n답변 작성: 검색 데이터의 reference에서 확인한 사실로 설명하고, "
                "설명한 문장 끝에 해당 자료 id를 [1] 형식으로 인용하세요. "
                "관련 official_api 내용도 설명하고 그 id를 반드시 인용하세요. "
                "수치·단위·기간·적용 조건은 자료 그대로 보존하고 추측하지 마세요. "
                "일반 지식만으로 답하거나 인용 없이 끝내지 마세요. "
                "관련 근거가 없으면 그 한계를 밝히세요.",
            }
        response = await self.request(
            "model",
            "POST",
            self.settings.local_llm_endpoint.rstrip("/") + "/v1/chat/completions",
            json={
                "model": self.generation_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "top_p": 0.9,
                "seed": 42,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        try:
            data = response.json()
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected one completion")
            choice = choices[0]
            text = choice["message"]["content"]
            reason = choice["finish_reason"]
            if not isinstance(text, str) or not text.strip() or reason not in {"stop", "length"}:
                raise ValueError("Invalid local completion")
            model = data["model"]
            if not isinstance(model, str) or not model:
                raise ValueError("Missing model identity")
        except (ValueError, KeyError, TypeError):
            logger.warning("Invalid local model completion")
            raise UpstreamError("model", 502) from None
        # Normalize the transport shape so shared citation/safety handling remains identical.
        return {
            "model": model,
            "status": "incomplete" if reason == "length" else "completed",
            "output": [{"content": [{"type": "output_text", "text": text.strip()}]}],
        }

    async def ready(self) -> None:
        await self.request("model", "GET", self.settings.local_llm_endpoint.rstrip("/") + "/health")
        for index in (self.knowledge_index, self.case_index):
            result = await self.request(
                "search",
                "GET",
                f"{self.search_endpoint.rstrip('/')}/indexes/{index}/docs/$count"
                f"?api-version={SEARCH_API}",
                headers={"Authorization": f"Bearer {await self.token(search=True)}"},
            )
            try:
                count = result.json()
            except ValueError:
                logger.warning("Azure comparison search returned an invalid document count")
                raise UpstreamError("search", 502) from None
            if type(count) is not int or count <= 0:
                logger.warning("Azure comparison search corpus is not ready")
                raise UpstreamError("search", 502)

    async def chat(
        self, payload: ChatRequest, *, trace: EvaluationTrace | None = None
    ) -> ChatResponse:
        result = await super().chat(payload, trace=trace)
        if trace is not None:
            trace.data["service_variant"] = self.settings.service_variant
            trace.data["core_execution"] = (
                "foundry-small-model-with-azure-search"
                if self.settings.service_variant == "qwen-azure-rag"
                else "foundry-models-with-oss-search"
            )
        return result


class LocalServices(QwenServices):
    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        if settings.service_variant not in {"local-rag", "local-only"}:
            raise ValueError("LocalServices requires an explicit local variant")
        super().__init__(settings, **kwargs)

    @property
    def search_endpoint(self) -> str:
        return self.settings.local_search_endpoint

    @property
    def knowledge_index(self) -> str:
        return self.settings.local_search_index

    @property
    def case_index(self) -> str:
        return self.settings.local_search_case_index

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.settings.service_variant != "local-rag":
            logger.error("Embedding is disabled for the model-only variant")
            raise UpstreamError("embedding")
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("Embedding inputs must not be empty")
        response = await self.request(
            "embedding",
            "POST",
            self.settings.local_embedding_endpoint.rstrip("/") + "/embed",
            json={"inputs": texts, "truncate": False, "input_type": "query"},
        )
        try:
            vectors = response.json()
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError("Invalid vector count")
            for vector in vectors:
                if (
                    not isinstance(vector, list)
                    or len(vector) != self.settings.embedding_dimensions
                ):
                    raise ValueError("Invalid vector dimensions")
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                    for value in vector
                ):
                    raise ValueError("Invalid vector values")
        except (ValueError, TypeError):
            logger.warning("Invalid local embedding response")
            raise UpstreamError("embedding", 502) from None
        result: list[list[float]] = vectors
        return result

    async def search_documents(self, index: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if self.settings.service_variant != "local-rag":
            logger.error("Search is disabled for the model-only variant")
            raise UpstreamError("search")
        from silvercare.offline_search import OfflineSearch

        return await OfflineSearch(
            self.client, self.search_endpoint, self.settings.local_reranker_endpoint
        ).query(index, payload)

    async def ready(self) -> None:
        await self.request("model", "GET", self.settings.local_llm_endpoint.rstrip("/") + "/health")
        if self.settings.service_variant == "local-rag":
            for index in (self.knowledge_index, self.case_index):
                result = await self.request(
                    "search", "GET", f"{self.search_endpoint.rstrip('/')}/{index}/_count"
                )
                if result.json().get("count", 0) <= 0:
                    logger.warning("Local search corpus is not ready")
                    raise UpstreamError("search")
            for endpoint, path in (
                (self.settings.local_embedding_endpoint, "/health"),
                (self.settings.local_reranker_endpoint, "/info"),
            ):
                await self.request("embedding", "GET", endpoint.rstrip("/") + path)

    async def chat(
        self, payload: ChatRequest, *, trace: EvaluationTrace | None = None
    ) -> ChatResponse:
        if self.settings.service_variant == "local-rag":
            return await super().chat(payload, trace=trace)
        emergency = emergency_response(payload.message)
        if emergency:
            if trace is not None:
                trace.data.update(route="fixed_emergency", service_variant="local-only")
            return emergency
        inputs = [{"role": m.role, "content": redact(m.content)} for m in payload.history]
        inputs.append({"role": "user", "content": redact(payload.message)})
        data = await self.generate(LLM_ONLY_PROMPT, inputs, 1300)
        answer = data["output"][0]["content"][0]["text"]
        if data["status"] != "completed" or re.search(r"\[\d+\]", answer):
            logger.warning("Model-only response is incomplete or contains unsupported citations")
            raise UpstreamError("model", 502)
        if trace is not None:
            trace.data.update(
                service_variant="local-only",
                core_execution="foundry-hosted-small-model",
                route="model_only",
                generation_called=True,
                generation_deployment=self.generation_model,
                actual_generation_model=data["model"],
                prompt_sha256=hashlib.sha256(LLM_ONLY_PROMPT.encode()).hexdigest(),
                model_reference_context=[],
                reference_identities=[],
                dialogue_example_count=0,
                qa_example_count=0,
            )
        caution = medical_caution(payload.message, answer)
        notice = MODEL_ONLY_NOTICE + " " + NOTICE
        return ChatResponse(
            answer=redact(answer),
            mode="conversation",
            sources=[],
            health_topics=[],
            notice=notice,
            medical_caution=caution,
            display_notice=MODEL_ONLY_NOTICE
            + " "
            + answer_display_notice(NOTICE, caution, dataset_cited=False, has_sources=False),
        )
