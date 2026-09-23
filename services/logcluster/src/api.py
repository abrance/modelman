"""HTTP 层：路由、鉴权、请求校验、错误码映射。

刻意保持与 NAS 上旧实现的路由一致（`/cluster`、`/match`、`/clusters`、
`/health`），再按仓库约定补上 `/livez`、`/healthz`、`/readyz`、`/version`、
`/models`、`/metrics`。业务端点的响应字段只增不改。

端点是同步 `def`：FastAPI 会把它们丢到线程池里执行，drain3 的 CPU 计算
因此不会占住事件循环。写成 `async def` 再直接算才是错的。
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

from . import build_info
from .config import Config
from .engine import DrainEngine, Overloaded, StateUnavailable
from .metrics import Metrics
from .models import (
    ClusterInfo,
    ClusterLine,
    ClusterRequest,
    ClusterResponse,
    ClustersResponse,
    HealthResponse,
    MatchLine,
    MatchRequest,
    MatchResponse,
    ModelInfo,
    VersionResponse,
)
from .state import StateError

logger = logging.getLogger(__name__)

SERVICE_NAME = "modelman-logcluster"

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


class BodyLimitExceeded(Exception):
    """ASGI 层体积超限。"""


class BodySizeLimitMiddleware:
    """把请求体读进内存并卡体积上限。

    只看 `content-length` 不够：header 可以撒谎或缺失。这里把 body 读完
    再交给下游，超过上限就整条拒掉，因此下游拿到的 body 一定是完整的、
    且不超过上限的（上限本身就是内存上界）。
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
                # http.disconnect 之类：当作没有更多 body
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
    response = JSONResponse(
        status_code=400, content={"success": False, "error": message, "detail": message}
    )
    await response({"type": "http", "method": "POST", "headers": []}, None, send)


def create_app(config: Config, engine: DrainEngine, metrics: Metrics) -> FastAPI:
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
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 仓库约定：请求体格式错误是 400，不是 FastAPI 默认的 422
        return JSONResponse(
            status_code=400, content=_error_body(_format_validation(exc))
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=_error_body(str(exc.detail))
        )

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
        clusters, messages = engine.counts()
        return HealthResponse(
            status="ok",
            models_loaded=[config.profile_id] if engine.is_ready() else [],
            uptime_secs=round(time.monotonic() - started, 3),
            cluster_count=clusters,
            total_size=messages,
        )

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        if engine.is_ready():
            return JSONResponse(
                status_code=200,
                content={"status": "ready", "profile": config.profile_id},
            )
        reason = engine.load_error or "状态不可用"
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "profile": config.profile_id,
                **_error_body(reason),
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
            state_dir=str(config.state_dir),
            profile_id=config.profile_id,
            sim_th=config.sim_th,
            depth=config.depth,
            max_children=config.max_children,
            mask_rules=list(config.mask_rules),
            parametrize_numeric=config.parametrize_numeric,
            snapshot_interval_minutes=config.snapshot_interval_minutes,
            max_concurrency=config.max_concurrency,
            queue_timeout_secs=config.queue_timeout_secs,
            limit_concurrency=config.limit_concurrency,
            max_lines=config.max_lines,
            max_line_chars=config.max_line_chars,
            max_bytes=config.max_bytes,
            auth_required=config.auth_token is not None,
            state_loaded=engine.state_loaded,
        )

    @app.get("/models", response_model=list[ModelInfo])
    def models() -> list[ModelInfo]:
        clusters, messages = engine.counts()
        state = engine.state_metadata()
        state["clusters"] = clusters
        state["messages"] = messages
        return [
            ModelInfo(
                id=config.profile_id,
                label=(
                    "Drain3 在线模板挖掘：无权重文件，"
                    "聚类参数由环境变量决定，状态持久化在 STATE_DIR"
                ),
                available=True,
                loaded=engine.is_ready(),
                load_error=engine.load_error,
                params=config.profile(),
                state=state,
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

    # ── 业务端点 ────────────────────────────────────────────────────────

    @app.post("/cluster", response_model=ClusterResponse, dependencies=guarded)
    def cluster(payload: ClusterRequest) -> ClusterResponse:
        def work():
            _validate_lines(payload.lines, config, metrics)
            return engine.cluster(payload.lines)

        outcome = _run("cluster", metrics, len(payload.lines), work)
        return ClusterResponse(
            success=True,
            results=[ClusterLine(**item) for item in outcome.results],
            cluster_count=outcome.cluster_count,
            time_ms=outcome.elapsed_ms,
        )

    @app.post("/match", response_model=MatchResponse, dependencies=guarded)
    def match(payload: MatchRequest) -> MatchResponse:
        def work():
            _validate_lines(payload.lines, config, metrics)
            return engine.match(payload.lines)

        outcome = _run("match", metrics, len(payload.lines), work)
        return MatchResponse(
            success=True,
            results=[MatchLine(**item) for item in outcome.results],
            time_ms=outcome.elapsed_ms,
        )

    @app.get("/clusters", response_model=ClustersResponse, dependencies=guarded)
    def clusters() -> ClustersResponse:
        items, total = _run("clusters", metrics, 0, engine.list_clusters)
        return ClustersResponse(
            success=True,
            clusters=[ClusterInfo(**item) for item in items],
            total_size=total,
        )

    return app


# ─── 内部 ───────────────────────────────────────────────────────────────────


def _run(endpoint: str, metrics: Metrics, lines: int, work: Callable[[], T]) -> T:
    """统一的指标记录与错误码映射。所有业务端点都经过这里。"""
    with metrics.in_flight():
        started = time.perf_counter()
        try:
            result = work()
        except Overloaded as exc:
            metrics.record_failure(endpoint, "overloaded")
            raise ApiError(503, str(exc)) from exc
        except StateUnavailable as exc:
            metrics.record_failure(endpoint, "state_unavailable")
            raise ApiError(503, str(exc)) from exc
        except StateError as exc:
            metrics.record_failure(endpoint, "state_error")
            raise ApiError(500, f"状态操作失败：{exc}") from exc
        except ApiError as exc:
            metrics.record_failure(endpoint, exc.status_label)
            raise
        except Exception as exc:
            metrics.record_failure(endpoint, "error")
            # logger.exception 自己会带上 traceback，不用把 exc 也格式化一遍
            logger.exception("%s failed", endpoint)
            raise ApiError(500, f"{endpoint} 失败：{exc}") from exc
        metrics.record_success(endpoint, time.perf_counter() - started, lines)
        return result


def _validate_lines(lines: list[str], config: Config, metrics: Metrics) -> None:
    if len(lines) > config.max_lines:
        metrics.record_rejected("too_many_lines")
        raise ApiError(
            400,
            f"一次最多处理 MAX_LINES={config.max_lines} 行日志，收到 {len(lines)} 行",
        )
    for index, line in enumerate(lines, start=1):
        if len(line) > config.max_line_chars:
            metrics.record_rejected("line_too_long")
            raise ApiError(
                400,
                f"第 {index} 行长度 {len(line)} 超过 MAX_LINE_CHARS={config.max_line_chars}",
            )


def _error_body(message: str) -> dict:
    # detail 是旧实现的字段名，保留一份给可能的既有调用方
    return {"success": False, "error": message, "detail": message}


def _format_validation(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(
            str(item) for item in error.get("loc", ()) if item != "body"
        )
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
