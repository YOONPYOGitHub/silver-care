import asyncio
import ipaddress
import logging
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import httpx
from azure.core.exceptions import ClientAuthenticationError
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from silvercare.azure import AzureServices, UpstreamError
from silvercare.config import Settings, get_settings
from silvercare.health import BY_ID, TOPICS, HealthService, HealthUnavailable, select_topics
from silvercare.models import ChatRequest, ChatResponse, HealthArticle, HealthTopic, SpeechRequest
from silvercare.tracing import EvaluationTrace

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
AUDIO_LIMIT = 4 * 1024 * 1024


def error_response(request: Request, body: dict[str, Any], status: int) -> JSONResponse:
    trace = getattr(request.state, "evaluation_trace", None)
    if isinstance(trace, EvaluationTrace):
        trace.data["failure"] = {"http_status": status, "code": body.get("code")}
        body = {**body, "evaluation_trace": trace.export()}
    return JSONResponse(body, status)


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = AUDIO_LIMIT + 8192 if scope["path"] == "/api/speech/transcribe" else 196608
        size = 0
        parts: list[Message] = []
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            size += len(event.get("body", b""))
            if size > limit:
                response = JSONResponse({"detail": "요청 크기가 너무 큽니다."}, 413)
                await response(scope, receive, send)
                return
            parts.append(event)
            if not event.get("more_body", False):
                break
        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(parts):
                item = parts[index]
                index += 1
                return item
            return await receive()

        await self.app(scope, replay, send)


class RateLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.clients: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> bool:
        now = time.monotonic()
        events = self.clients.setdefault(key, deque())
        self.clients.move_to_end(key)
        while events and events[0] <= now - 60:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        if len(self.clients) > 10000:
            self.clients.popitem(last=False)
        return True


def create_app(settings: Settings | None = None, services: AzureServices | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.health = HealthService(config.kdca_health_api_key)
        app.state.azure = services or AzureServices(config, health_service=app.state.health)
        try:
            yield
        finally:
            try:
                await app.state.azure.close()
            finally:
                await app.state.health.close()

    app = FastAPI(
        title="온담 · 실버케어",
        lifespan=lifespan,
        docs_url="/docs" if config.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.app_env != "production" else None,
    )
    app.add_middleware(BodyLimit)
    limiter = RateLimiter(config.rate_limit_per_minute)
    capacity = asyncio.Semaphore(config.max_concurrent_requests)

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Response:
        request_id = uuid4().hex[:12]
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != config.public_origin.rstrip("/"):
                return JSONResponse({"detail": "허용되지 않은 요청 출처입니다."}, 403)
        if request.url.path.startswith("/api/"):
            client = request.client.host if request.client else "unknown"
            if config.trusted_proxy:
                try:
                    client = str(ipaddress.ip_address(request.headers.get("x-real-ip", client)))
                except ValueError:
                    return JSONResponse({"detail": "잘못된 프록시 요청입니다."}, 400)
            if not limiter.check(client):
                return JSONResponse(
                    {"detail": "잠시 쉬었다가 다시 시도해 주세요."},
                    429,
                    headers={"Retry-After": "60"},
                )
        response: Response = await call_next(request)
        if "silvercare_session" in request.cookies:
            response.delete_cookie("silvercare_session", path="/")
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                ),
                "Cache-Control": "no-store" if request.url.path.startswith("/api/") else "no-cache",
            }
        )
        if config.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        if exc.service == "model" and exc.code == "content_filter":
            return error_response(
                request,
                {
                    "code": "safety_refusal",
                    "detail": (
                        "안전 정책에 따라 이 질문에는 답변을 제공할 수 없어요. "
                        "건강이나 약 복용에 관한 판단은 의료진에게 확인해 주세요."
                    ),
                },
                422,
            )
        names = {
            "search": "자료 검색",
            "model": "AI 답변",
            "speech": "음성",
            "embedding": "자료 이해",
        }
        message = f"{names.get(exc.service, '연결')} 서비스 연결에 문제가 있습니다."
        if exc.service == "speech" and exc.status == 422:
            return error_response(
                request, {"detail": "말씀을 잘 듣지 못했어요. 다시 한 번 말씀해 주세요."}, 422
            )
        return error_response(request, {"detail": message + " 잠시 후 다시 시도해 주세요."}, 503)

    @app.exception_handler(ClientAuthenticationError)
    async def auth_error(request: Request, exc: ClientAuthenticationError) -> JSONResponse:
        logger.error("Azure authentication unavailable: %s", type(exc).__name__)
        return error_response(request, {"detail": "서비스 연결 인증을 확인해 주세요."}, 503)

    @app.exception_handler(HealthUnavailable)
    async def health_error(request: Request, exc: HealthUnavailable) -> JSONResponse:
        logger.warning("Official health information unavailable: %s", exc.reason)
        return error_response(
            request,
            {
                "detail": (
                    "공식 건강정보를 불러오지 못했어요. "
                    "잠시 후 다시 시도하거나 원문 링크를 이용해 주세요."
                )
            },
            503,
        )

    @app.exception_handler(httpx.RequestError)
    async def transport_error(request: Request, exc: httpx.RequestError) -> JSONResponse:
        logger.warning("External transport unavailable: %s", type(exc).__name__)
        return error_response(request, {"detail": "외부 서비스에 연결하지 못했습니다."}, 503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Validation input can contain health information.
        return JSONResponse({"detail": "입력 형식이나 길이를 확인해 주세요."}, 422)

    async def slot() -> AsyncIterator[None]:
        if capacity.locked():
            raise HTTPException(429, "상담이 몰리고 있어요. 잠시 후 다시 시도해 주세요.")
        async with capacity:
            yield

    guarded = [Depends(slot)]

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def public_config() -> dict[str, Any]:
        return {
            "name": "온담",
            "search_enabled": bool(config.azure_search_endpoint),
            "voice_enabled": bool(config.azure_speech_resource_id),
            "health_info_enabled": bool(config.kdca_health_api_key.get_secret_value()),
        }

    @app.post("/api/chat", response_model=ChatResponse, dependencies=guarded)
    async def chat(payload: ChatRequest, request: Request) -> ChatResponse | JSONResponse:
        trace = None
        if request.headers.get("x-ondam-evaluation") == "trace":
            expected = config.evaluation_trace_token.get_secret_value()
            authorization = request.headers.get("authorization", "")
            if (
                not config.evaluation_trace_enabled
                or not expected
                or not secrets.compare_digest(authorization.encode(), f"Bearer {expected}".encode())
            ):
                raise HTTPException(403, "평가 추적 권한을 확인해 주세요.")
            trace = EvaluationTrace.for_request(payload)
            request.state.evaluation_trace = trace
        result: ChatResponse
        if trace is None:
            result = await request.app.state.azure.chat(payload)
        else:
            result = await request.app.state.azure.chat(payload, trace=trace)
        if result.mode != "emergency" and config.kdca_health_api_key.get_secret_value():
            result.health_topics = select_topics(payload.message, payload.history)
        if trace is not None:
            trace.data["response_mode"] = result.mode
            return JSONResponse(
                {**result.model_dump(mode="json"), "evaluation_trace": trace.export()}
            )
        return result

    @app.get("/api/health-topics", response_model=list[HealthTopic])
    async def health_topics() -> list[HealthTopic]:
        if not config.kdca_health_api_key.get_secret_value():
            return []
        return [topic.public() for topic in TOPICS]

    @app.get("/api/health-info/{content_id}", response_model=HealthArticle, dependencies=guarded)
    async def health_article(content_id: str, request: Request) -> HealthArticle:
        if content_id not in BY_ID:
            raise HTTPException(404, "선택한 건강정보를 찾을 수 없어요.")
        article: HealthArticle = await request.app.state.health.article(content_id)
        return article

    @app.post("/api/speech/synthesize", dependencies=guarded)
    async def synthesize(payload: SpeechRequest, request: Request) -> Response:
        content = await request.app.state.azure.synthesize(payload.text)
        return Response(content, media_type="audio/mpeg")

    @app.post("/api/speech/transcribe", dependencies=guarded)
    async def transcribe(request: Request, audio: Annotated[UploadFile, File()]) -> dict[str, str]:
        content_type = (audio.content_type or "").split(";")[0]
        if content_type not in {"audio/webm", "audio/ogg", "audio/wav", "audio/mp4"}:
            raise HTTPException(415, "지원하지 않는 음성 형식입니다.")
        content = await audio.read(AUDIO_LIMIT + 1)
        await audio.close()
        if not content:
            raise HTTPException(422, "말씀이 들리지 않았어요. 다시 한 번 말씀해 주세요.")
        if len(content) > AUDIO_LIMIT:
            raise HTTPException(413, "음성 크기가 처리 한도(4MB)를 넘었어요. 나누어 말씀해 주세요.")
        text = await request.app.state.azure.transcribe(content, content_type)
        return {"text": text}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
