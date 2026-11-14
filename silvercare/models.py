from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from silvercare.speech_text import spoken_text


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list, max_length=12)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("메시지를 입력해 주세요.")
        return value.strip()


WHO_REUSE_NOTICE = (
    "출처: World Health Organization, Integrated care for older people (ICOPE), second edition, "
    "ISBN 9789240103726. © WHO 2024; publication page: 2025-09-22. "
    "원문: CC BY-NC-SA 3.0 IGO, https://creativecommons.org/licenses/by-nc-sa/3.0/igo/. "
    "발췌·정제·한국어 번역·요약 부분: CC BY-NC-SA 4.0, "
    "https://creativecommons.org/licenses/by-nc-sa/4.0/, 비상업적 이용. "
    "This is an adaptation/translation of the original work. It was not created by WHO. "
    "WHO is not responsible for its content or accuracy. "
    "The original English edition is the binding and authentic edition. No WHO endorsement."
)


class Source(BaseModel):
    id: int
    title: str
    url: str
    excerpt: str
    updated_at: str
    evidence_kind: str = "reference"
    record_id: str = ""
    source_display: str = ""
    source_year: str = ""
    license: str = ""
    license_url: str = ""
    reuse_notice: str = ""
    source_type: str = ""
    source_language: str = ""
    jurisdiction: str = ""
    context_record_ids: list[str] = Field(default_factory=list)


class HealthTopic(BaseModel):
    id: str
    title: str
    url: str


class HealthSection(BaseModel):
    title: str
    text: str


class HealthArticle(BaseModel):
    topic: HealthTopic
    sections: list[HealthSection]
    fetched_at: str
    source_display: str = "질병관리청 국가건강정보포털"
    notice: str = (
        "공식 자료의 텍스트를 발췌한 것으로 AI가 요약·재작성한 답변이 아닙니다. "
        "표·그림·영상과 전체 내용은 원문에서 확인해 주세요. "
        "건강정보는 참고용이며 개인별 진단·복약 변경·검사 결정은 의료진과 상의해 주세요."
    )
    license_notice: str = "공공누리 제4유형: 출처표시·상업적 이용금지·변경금지"


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source] = Field(default_factory=list)
    mode: Literal["grounded", "conversation", "emergency"]
    notice: str
    medical_caution: str | None = None
    display_notice: str = ""
    health_topics: list[HealthTopic] = Field(default_factory=list)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def speech_body(cls, value: str) -> str:
        text = spoken_text(value)
        if not text:
            raise ValueError("읽을 답변 내용이 없습니다.")
        return text
