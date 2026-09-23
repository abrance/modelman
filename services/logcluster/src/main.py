"""进程入口。

启动顺序：解析配置 → 确认状态目录可写 → 建指标与引擎 → 起 HTTP。
配置与状态目录问题直接退出 2（部署错误，重启也不会变好）；
状态文件本身不兼容只降级，不退出——原因会出现在 /models 与 /readyz 上，
进程活着才有机会用 HTTP 去看。
"""

from __future__ import annotations

import logging
import sys
import time

import uvicorn

from . import build_info
from .api import SERVICE_NAME, create_app
from .config import Config, ConfigError
from .engine import DrainEngine
from .metrics import Metrics
from .state import StateError, ensure_writable

logger = logging.getLogger(__name__)


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    _configure_logging(config.log_level)

    try:
        ensure_writable(config.state_dir)
    except StateError as exc:
        print(f"state error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    info = build_info.load()
    metrics = Metrics(version=info.version, commit=info.git_commit)
    engine = DrainEngine(config, metrics)
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
        "listen=%s state_dir=%s profile=%s max_concurrency=%d "
        "limit_concurrency=%d max_lines=%d max_bytes=%d auth_required=%s state_loaded=%s",
        config.listen_addr,
        config.state_dir,
        config.profile(),
        config.max_concurrency,
        config.limit_concurrency,
        config.max_lines,
        config.max_bytes,
        config.auth_token is not None,
        engine.state_loaded,
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
        # 收尾快照：drain3 在模板变化时会自己存，这里补一次退出时的保存
        saved = engine.save_state("exit")
        logger.info(
            "stopped after %.1fs, state_saved=%s", time.monotonic() - started, saved
        )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    main()
