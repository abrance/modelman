"""进程入口。

配置问题直接退出 2；模型加载失败只降级，原因出现在 /models 与 /readyz 上。
"""

from __future__ import annotations

import logging
import sys
import time

import uvicorn

from . import build_info
from .api import SERVICE_NAME, create_app
from .config import Config, ConfigError
from .engine import PriceEngine
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
    engine = PriceEngine(config)
    app = create_app(config, engine, metrics)

    host, port = config.host_port
    logger.info(
        "%s %s (%s, built %s) starting",
        SERVICE_NAME, info.version, info.git_commit, info.build_time,
    )
    logger.info(
        "listen=%s models_dir=%s tier=%s auth_required=%s ready=%s",
        config.listen_addr, config.models_dir, config.model_tier,
        config.auth_token is not None, engine.is_ready(),
    )

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=config.log_level,
            log_config=None,
            access_log=True,
            limit_concurrency=32,
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
