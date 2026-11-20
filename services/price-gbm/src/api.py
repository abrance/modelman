"""HTTP 层：路由、鉴权、请求校验、错误码映射。

端点按仓库约定实现：`/`（页面）、`/livez`、`/healthz`、`/readyz`、`/version`、
`/models`、`/metrics`（需鉴权），业务端点 `POST /v1/price-forecast`。
同步 `def` 端点跑在线程池里，GBM 的 CPU 计算不占事件循环。
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import TypeVar

import numpy as np
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import build_info, ui
from .config import Config
from .engine import InvalidRequest, ModelUnavailable, PriceEngine
from .metrics import Metrics
from .models import (
    DayItem,
    HealthResponse,
    ModelInfo,
    PriceForecastRequest,
    PriceForecastResponse,
    VersionResponse,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "modelman-price-gbm"

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
    """把请求体读进内存并卡体积上限（与 forecast 同款）。"""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = None
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
        if declared is not None and declared > self.max_bytes:
            await self._reject(send, declared)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(send, len(body))
                return
            if not message.get("more_body", False):
                break

        payload = bytes(body)
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": payload, "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, send, received: int) -> None:
        message = f"请求体超过 MAX_BYTES={self.max_bytes}（已收到 {received} 字节）"
        response = JSONResponse(status_code=400, content=_error_body(message))
        await response({"type": "http", "method": "POST", "headers": []}, None, send)


def create_app(config: Config, engine: PriceEngine, metrics: Metrics) -> FastAPI:
    started = time.monotonic()
    info = build_info.load()

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

    # ── 鉴权 ────────────────────────────────────────────────────────

    def require_auth(request: Request) -> None:
        expected = config.auth_token
        if expected is None:
            return
        provided = _token_from_headers(request)
        if provided is None or not hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        ):
            raise ApiError(401, "missing or invalid auth token")

    guarded = [Depends(require_auth)]

    # ── 异常处理 ────────────────────────────────────────────────────

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=_error_body(exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_error_body(_format_validation(exc)))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error_body(str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content=_error_body(f"内部错误：{exc}"))

    # ── 探活与状态 ──────────────────────────────────────────────────

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
                **_error_body(engine.load_error or "模型未就绪"),
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
            auth_required=config.auth_token is not None,
        )

    @app.get("/models", response_model=list[ModelInfo])
    def models() -> list[ModelInfo]:
        return [
            ModelInfo(
                id=config.model_tier,
                label=(
                    "LightGBM 分位回归（q10/q50/q90）：广东现货日前小时电价，"
                    "特征含同小时滞后/滚动统计/电量/日历/趋势，权重随镜像交付"
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

    # ── 自带页面 ────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    def ui_index() -> Response:
        return ui.asset()

    # ── 业务端点 ────────────────────────────────────────────────────

    @app.post("/v1/price-forecast", response_model=PriceForecastResponse, dependencies=guarded)
    def price_forecast(payload: PriceForecastRequest) -> PriceForecastResponse:
        started_at = time.perf_counter()
        try:
            outcome = engine.forecast(payload.days, payload.target)
        except InvalidRequest as exc:
            metrics.record_failure("price_forecast", "bad_request")
            raise ApiError(400, str(exc)) from exc
        except ModelUnavailable as exc:
            metrics.record_failure("price_forecast", "model_unavailable")
            raise ApiError(503, str(exc)) from exc
        except Exception as exc:
            metrics.record_failure("price_forecast", "error")
            logger.exception("price_forecast failed")
            raise ApiError(500, f"price_forecast 失败：{exc}") from exc
        metrics.record_success("price_forecast", time.perf_counter() - started_at)
        return PriceForecastResponse(
            model=config.model_tier,
            target=outcome.target,
            horizon_days=outcome.horizon_days,
            point=_to_json(outcome.point),
            quantiles={level: _to_json(values) for level, values in outcome.quantiles.items()},
            time_ms=round(outcome.elapsed_ms, 3),
        )

    return app


# ─── 内部 ───────────────────────────────────────────────────────────────────


def _to_json(array: np.ndarray) -> list[float]:
    return [float(value) for value in array]


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
        return authorization[len("Bearer "):].strip()
    return None
