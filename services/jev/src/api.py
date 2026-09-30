"""HTTP 层：路由、鉴权、请求校验、错误码映射。

端点按仓库约定实现：`/livez`、`/healthz`、`/readyz`、`/version`、`/models`、
`/metrics`（需鉴权），加上业务端点 `POST /v1/systemone`——它是 TypeSafe Jev 的
公开协议形状，`pi-jev` 这类既有调用方只要把 base url 指过来就能用，不改代码。

端点是同步 `def`：FastAPI 会把它们丢到线程池里执行，Laya 的 CPU 计算因此不会
占住事件循环。
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import build_info, ui
from .config import Config
from .engine import (
    DecisionEngine,
    InvalidRequest,
    ModelUnavailable,
    Overloaded,
    validate_request,
)
from .metrics import Metrics
from .models import (
    HealthResponse,
    ModelInfo,
    SystemOneRequest,
    SystemOneResponse,
    VersionResponse,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "modelman-jev"

T = TypeVar("T")


class ApiError(Exception):
    """带状态码的业务错误。由异常处理器统一渲染成 JSON。"""

    def __init__(self, status: int, message: str, label: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.status_label = label or {
            400: "bad_request",
            401: "unauthorized",
            500: "error",
            503: "unavailable",
        }.get(status, f"status_{status}")


class BodySizeLimitMiddleware:
    """把请求体读进内存并卡体积上限。

    只看 `content-length` 不够：header 可以撒谎或缺失。这里把 body 读完再交给
    下游，超过上限就整条拒掉，因此下游拿到的 body 一定完整且不超过上限
    （上限本身就是内存上界）。
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _reject_too_large(send, self.max_bytes, declared)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await _reject_too_large(send, self.max_bytes, len(body))
                return
            if not message.get("more_body", False):
                break

        replayed = False
        payload = bytes(body)

        async def replay():
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": payload, "more_body": False}

        await self.app(scope, replay, send)


def _content_length(scope) -> int | None:
    for key, value in scope.get("headers", []):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject_too_large(send, max_bytes: int, received: int) -> None:
    message = f"请求体超过 MAX_BYTES={max_bytes}（已收到 {received} 字节）"
    response = JSONResponse(status_code=400, content=_error_body(message))
    await response({"type": "http", "method": "POST", "headers": []}, None, send)


def create_app(config: Config, engine: DecisionEngine, metrics: Metrics) -> FastAPI:
    started = time.monotonic()
    info = build_info.load()

    # 关掉自带的 /docs 与 /openapi.json，改成一个需要鉴权的 openapi.json：
    # 除探活端点外不留任何免鉴权的面。
    app = FastAPI(
        title=SERVICE_NAME,
        version=info.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.max_bytes)
    app.state.config = config
    app.state.engine = engine
    app.state.metrics = metrics

    # ── 鉴权 ────────────────────────────────────────────────────────────

    def require_auth(request: Request) -> None:
        expected = config.auth_token
        if expected is None:
            return
        provided = _token_from_headers(request)
        if provided is None or not _constant_time_eq(provided, expected):
            raise ApiError(401, "missing or invalid auth token")

    guarded = [Depends(require_auth)]

    # ── 异常处理 ────────────────────────────────────────────────────────

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=_error_body(exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 仓库约定：请求体格式错误是 400，不是 FastAPI 默认的 422
        return JSONResponse(status_code=400, content=_error_body(_format_validation(exc)))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error_body(str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content=_error_body(f"内部错误：{exc}"))

    # ── 探活与状态 ──────────────────────────────────────────────────────

    @app.get("/livez")
    def livez() -> dict:
        return {"status": "ok"}

    @app.get("/healthz", response_model=HealthResponse)
    @app.get("/health", response_model=HealthResponse, include_in_schema=False)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            models_loaded=[config.model_tier] if engine.is_ready() else [],
            uptime_secs=round(time.monotonic() - started, 3),
        )

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        if engine.is_ready():
            return JSONResponse(
                status_code=200, content={"status": "ready", "tier": config.model_tier}
            )
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "tier": config.model_tier,
                **_error_body(engine.load_error or "权重未就绪"),
            },
        )

    @app.get("/version", response_model=VersionResponse)
    def version() -> VersionResponse:
        return VersionResponse(
            name=SERVICE_NAME,
            version=info.version,
            git_commit=info.git_commit,
            build_time=info.build_time,
            listen_addr=config.listen_addr,
            models_dir=str(config.models_dir),
            tier=config.model_tier,
            device=config.device,
            infer_threads=config.infer_threads,
            preload=config.preload,
            max_len=config.max_len,
            head_max_len=config.head_max_len,
            max_state_chars=config.max_state_chars,
            max_questions=config.max_questions,
            max_options=config.max_options,
            max_bytes=config.max_bytes,
            max_concurrency=config.max_concurrency,
            queue_timeout_secs=config.queue_timeout_secs,
            limit_concurrency=config.limit_concurrency,
            auth_required=config.auth_token is not None,
        )

    @app.get("/models", response_model=list[ModelInfo])
    def models() -> list[ModelInfo]:
        return [
            ModelInfo(
                id=config.model_tier,
                label=(
                    "Laya System One 判定模型：typed choice/noul/score，"
                    "权重随镜像交付，单次前向出全部答案"
                ),
                available=True,
                loaded=engine.is_ready(),
                load_error=engine.load_error,
                params=config.profile(),
                state=engine.model_metadata(),
            )
        ]

    @app.get("/metrics", dependencies=guarded)
    def prometheus() -> Response:
        return Response(
            content=metrics.render(uptime_secs=time.monotonic() - started),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/openapi.json", include_in_schema=False, dependencies=guarded)
    def openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    # ── 自带页面 ────────────────────────────────────────────────────────
    # 免鉴权是刻意的：页面得先能打开，才谈得上填 token。三条路由只暴露页面
    # 结构，不含任何数据。

    @app.get("/", include_in_schema=False)
    def ui_index() -> Response:
        return ui.asset(ui.INDEX_HTML, "text/html")

    @app.get("/app.css", include_in_schema=False)
    def ui_css() -> Response:
        return ui.asset(ui.APP_CSS, "text/css")

    @app.get("/app.js", include_in_schema=False)
    def ui_js() -> Response:
        return ui.asset(ui.APP_JS, "application/javascript")

    # ── 业务端点 ────────────────────────────────────────────────────────

    @app.post("/v1/systemone", response_model=SystemOneResponse, dependencies=guarded)
    def systemone(payload: SystemOneRequest) -> SystemOneResponse:
        # 先把 pydantic 模型还原成 dict：校验层与引擎只认上游的原始 schema
        questions = {
            qid: _question_payload(question)
            for qid, question in payload.questions.items()
        }

        def work():
            validate_request(payload.state, questions, config)
            return engine.decide(payload.state, questions)

        outcome = _run("systemone", metrics, len(payload.questions), work)
        return SystemOneResponse(
            success=True,
            model=outcome.model,
            answers=outcome.answers,
            usage=_usage_with_total(outcome.usage),
            time_ms=round(outcome.elapsed_ms, 3),
            truncated=outcome.truncated,
        )

    return app


# ─── 内部 ───────────────────────────────────────────────────────────────────


def _question_payload(question) -> dict:
    """把 pydantic 模型还原成上游期望的 dict，丢掉未设置的可选字段。"""
    payload = question.model_dump(exclude_none=True)
    return payload


def _usage_with_total(usage: dict) -> dict:
    """补一个 `totalTokens` 键。

    上游 Jev 的 usage 用 camelCase 的 `totalTokens`，Laya 用 `input_tokens` /
    `output_tokens`。既有调用方（pi-jev 的统计）读的是前者，所以两个都留着：
    契约字段只增不改。
    """
    enriched = dict(usage)
    if "totalTokens" not in enriched:
        total = enriched.get("input_tokens", 0) + enriched.get("output_tokens", 0)
        enriched["totalTokens"] = total
    return enriched


def _run(endpoint: str, metrics: Metrics, questions: int, work: Callable[[], T]) -> T:
    """统一的指标记录与错误码映射。所有业务端点都经过这里。"""
    with metrics.in_flight():
        started = time.perf_counter()
        try:
            result = work()
        except InvalidRequest as exc:
            metrics.record_failure(endpoint, "bad_request")
            raise ApiError(400, str(exc)) from exc
        except Overloaded as exc:
            metrics.record_failure(endpoint, "overloaded")
            raise ApiError(503, str(exc)) from exc
        except ModelUnavailable as exc:
            metrics.record_failure(endpoint, "model_unavailable")
            raise ApiError(503, str(exc)) from exc
        except ApiError as exc:
            metrics.record_failure(endpoint, exc.status_label)
            raise
        except Exception as exc:
            metrics.record_failure(endpoint, "error")
            logger.exception("%s failed", endpoint)
            raise ApiError(500, f"{endpoint} 失败：{exc}") from exc
        tokens = getattr(result, "usage", None)
        metrics.record_success(endpoint, time.perf_counter() - started, questions, tokens)
        return result


def _error_body(message: str) -> dict:
    return {"success": False, "error": message, "detail": message}


def _format_validation(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
        parts.append(f"{location or 'body'}: {error.get('msg', 'invalid')}")
    return "请求体校验失败：" + "；".join(parts)


def _token_from_headers(request: Request) -> str | None:
    header = request.headers.get("x-auth-token")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :].strip()
    return None


def _constant_time_eq(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
