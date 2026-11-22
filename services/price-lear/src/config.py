"""配置：环境变量只在这里读，其余代码只接受 Config 结构体。

默认值在开发机上开箱跑通：MODELS_DIR 指向服务目录下的 models/。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LISTEN_ADDR = "127.0.0.1:8080"
DEFAULT_MAX_DAYS = 60
DEFAULT_MAX_HORIZON_DAYS = 14
DEFAULT_MAX_BYTES = 2 << 20


class ConfigError(Exception):
    """配置非法。进程入口折算成退出码 2（重启不会变好）。"""


def parse_listen_addr(raw: str) -> tuple[str, int]:
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError(f"LISTEN_ADDR 非法：{raw!r}（应为 host:port）")
    return host, int(port)


@dataclass(frozen=True)
class Config:
    listen_addr: str
    models_dir: Path
    model_tier: str
    auth_token: str | None
    log_level: str
    max_days: int
    max_horizon_days: int
    max_bytes: int
    verify_weights: bool

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Config":
        get = (env or os.environ).get

        models_dir = get("MODELS_DIR", str(Path(__file__).resolve().parent.parent / "models"))
        max_days = _int(get("MAX_DAYS"), DEFAULT_MAX_DAYS, "MAX_DAYS")
        max_horizon = _int(get("MAX_HORIZON_DAYS"), DEFAULT_MAX_HORIZON_DAYS, "MAX_HORIZON_DAYS")
        max_bytes = _int(get("MAX_BYTES"), DEFAULT_MAX_BYTES, "MAX_BYTES")

        return Config(
            listen_addr=get("LISTEN_ADDR", DEFAULT_LISTEN_ADDR),
            models_dir=Path(models_dir),
            model_tier=get("MODEL_TIER", "lear-hour-v1"),
            auth_token=(get("AUTH_TOKEN") or "").strip() or None,
            log_level=get("LOG_LEVEL", "info"),
            max_days=max_days,
            max_horizon_days=max_horizon,
            max_bytes=max_bytes,
            verify_weights=_bool(get("VERIFY_WEIGHTS", "true")),
        )

    @property
    def host_port(self) -> tuple[str, int]:
        return parse_listen_addr(self.listen_addr)

    def profile(self) -> dict:
        return {
            "listen_addr": self.listen_addr,
            "max_days": self.max_days,
            "max_horizon_days": self.max_horizon_days,
            "max_bytes": self.max_bytes,
            "verify_weights": self.verify_weights,
        }


def _int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} 非法：{raw!r}（应为整数）") from None


def _bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")
