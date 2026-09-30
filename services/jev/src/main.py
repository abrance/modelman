"""进程入口。

启动顺序：解析配置 → 建指标与引擎（可选预加载）→ 起 HTTP。
配置问题直接退出 2（部署错误，重启也不会变好）；权重加载失败只降级，不退出——
原因会出现在 `/models` 与 `/readyz` 上，进程活着才有机会用 HTTP 去看。
"""

from __future__ import annotations

import logging
import sys
import time

import uvicorn

from . import build_info
from .api import SERVICE_NAME, create_app
from .config import Config, ConfigError
from .engine import DecisionEngine
from .metrics import Metrics

logger = logging.getLogger(__name__)


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    _configure_logging(config.log_level)

    info = build_info.load()
    metrics = Metrics(version=info.version, commit=info.git_commit)
    engine = DecisionEngine(config, metrics)
    app = create_app(config, engine, metrics)

    host, port = config.host_port
    logger.info(
        "%s %s (%s, built %s) starting",
        SERVICE_NAME,
        info.version,
        info.git_commit,
        info.build_time,
    )
    logger.info(
        "listen=%s models_dir=%s tier=%s device=%s threads=%d preload=%s "
        "max_concurrency=%d limit_concurrency=%d max_questions=%d max_bytes=%d "
        "auth_required=%s ready=%s",
        config.listen_addr,
        config.models_dir,
        config.model_tier,
        config.device,
        config.infer_threads,
        config.preload,
        config.max_concurrency,
        config.limit_concurrency,
        config.max_questions,
        config.max_bytes,
        config.auth_token is not None,
        engine.is_ready(),
    )

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=config.log_level,
            # 保留我们自己的 logging 配置，不用 uvicorn 的 dictConfig
            log_config=None,
            access_log=True,
            limit_concurrency=config.limit_concurrency,
            timeout_graceful_shutdown=10,
        )
    )

    started = time.monotonic()
    try:
        server.run()
    finally:
        logger.info("stopped after %.1fs", time.monotonic() - started)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    main()
