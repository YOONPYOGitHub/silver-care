from functools import lru_cache
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DataSourceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True, populate_by_name=True
    )

    aihub_api_key: SecretStr = SecretStr("")
    welfare_central_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "WELFARE_CENTRAL_API_KEY", "WELFARE_API_KEY", "welfare_api_key"
        ),
    )
    welfare_local_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "WELFARE_LOCAL_API_KEY", "WELFARE_API_KEY", "welfare_api_key"
        ),
    )
    nhis_ltc_search_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "NHIS_LTC_SEARCH_API_KEY", "NHIS_LTC_API_KEY", "nhis_ltc_api_key"
        ),
    )
    nhis_ltc_detail_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "NHIS_LTC_DETAIL_API_KEY", "NHIS_LTC_API_KEY", "nhis_ltc_api_key"
        ),
    )
    welfare_central_api_endpoint: str = (
        "https://apis.data.go.kr/B554287/NationalWelfareInformationsV001"
    )
    welfare_local_api_endpoint: str = (
        "https://apis.data.go.kr/B554287/LocalGovernmentWelfareInformations"
    )
    nhis_ltc_search_api_endpoint: str = "https://apis.data.go.kr/B550928/searchLtcInsttService02"
    nhis_ltc_detail_api_endpoint: str = (
        "https://apis.data.go.kr/B550928/getLtcInsttDetailInfoService02"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

    app_env: str = "development"
    public_origin: str = "http://localhost:8000"
    service_variant: Literal["azure", "local-rag", "local-only", "qwen-azure-rag"] = "azure"
    base_path: str = Field(default="", pattern=r"^(?:/[a-z][a-z0-9-]*)?$")
    local_llm_endpoint: str = ""
    local_llm_model: str = "qwen3.5-4b"
    local_search_endpoint: str = ""
    local_search_index: str = "local-knowledge-v1"
    local_search_case_index: str = "local-cases-v1"
    local_embedding_endpoint: str = ""
    local_reranker_endpoint: str = ""
    local_request_timeout: int = Field(default=600, ge=30, le=900)
    azure_openai_endpoint: str = "https://ai-foundry-m098047.openai.azure.com"
    azure_openai_deployment: str = "gpt-6.1-terra"
    azure_search_endpoint: str = ""
    azure_search_index: str = "silvercare"
    azure_search_case_index: str = ""
    retrieval_mode: Literal["keyword", "hybrid"] = "keyword"
    azure_embedding_deployment: str = ""
    embedding_dimensions: int = Field(default=512, ge=256, le=1536)
    semantic_ranking: bool = False
    rewrite_queries: bool = False
    prefer_official_references: bool = False
    noncommercial_data_use: bool = False
    contextual_retrieval: bool = False
    evaluation_trace_enabled: bool = False
    evaluation_trace_token: SecretStr = SecretStr("")
    azure_speech_endpoint: str = "https://ai-foundry-m098047.cognitiveservices.azure.com"
    azure_speech_resource_id: str = ""
    azure_speech_region: str = "koreacentral"
    azure_speech_voice: str = "ko-KR-SunHiNeural"
    rate_limit_per_minute: int = Field(default=12, ge=1, le=120)
    max_concurrent_requests: int = Field(default=4, ge=1, le=20)
    trusted_proxy: bool = False
    kdca_health_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("KDCA_HEALTH_API_KEY", "NHIS_HEALTH_INFO_API_KEY"),
    )

    @model_validator(mode="after")
    def production_guards(self) -> Self:
        if (
            self.evaluation_trace_enabled
            and len(self.evaluation_trace_token.get_secret_value()) < 32
        ):
            raise ValueError("Evaluation traces require a separate token of at least 32 characters")
        if (
            self.service_variant in {"azure", "qwen-azure-rag"}
            and self.retrieval_mode == "hybrid"
            and not self.azure_embedding_deployment
        ):
            raise ValueError("Hybrid retrieval requires AZURE_EMBEDDING_DEPLOYMENT")
        if self.service_variant != "azure" and not self.local_llm_endpoint:
            raise ValueError("Local variants require LOCAL_LLM_ENDPOINT")
        if self.service_variant != "azure" and not self.local_llm_model.strip():
            raise ValueError("Comparison variants require a model alias")
        if self.service_variant == "qwen-azure-rag" and not all(
            (
                self.azure_search_endpoint.strip(),
                self.azure_search_index.strip(),
                self.azure_search_case_index.strip(),
                self.azure_embedding_deployment.strip(),
            )
        ):
            raise ValueError("Qwen Azure RAG requires Azure Search indexes and embedding settings")
        if self.service_variant == "local-rag" and not all(
            (
                self.local_search_endpoint,
                self.local_embedding_endpoint,
                self.local_reranker_endpoint,
                self.local_search_index.strip(),
                self.local_search_case_index.strip(),
            )
        ):
            raise ValueError("Local RAG requires search, embedding, reranker and index settings")
        if (
            self.service_variant == "local-rag"
            and self.local_search_index == self.local_search_case_index
        ):
            raise ValueError("Knowledge and case indexes must remain separate")
        if self.app_env == "production":
            if not self.public_origin.startswith("https://"):
                raise ValueError("Production requires an HTTPS PUBLIC_ORIGIN")
            if (
                self.service_variant in {"azure", "qwen-azure-rag"}
                and not self.azure_search_endpoint
            ):
                raise ValueError("Production requires AZURE_SEARCH_ENDPOINT")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
