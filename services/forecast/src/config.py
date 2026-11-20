"""环境变量解析。这是唯一读环境变量的模块。

其余模块只接受 `Config`，测试可以直接构造它而不用改进程环境。
默认值保证在开发机上开箱能跑：`MODELS_DIR` 缺省是服务目录下的 `models/`。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LISTEN_ADDR = "0.0.0.0:8080"
DEFAULT_MODELS_DIR = "models"

# 唯一交付的档位。TimesFM 3.0 与 2.5 是两套不同的权重与 API，
# 同时常驻要双份内存（实测 3.0 单档位加载峰值 2.8 GB），不做路由。
DEFAULT_MODEL_TIER = "timesfm-3.0"
TIER_SUBDIRS = {
    "timesfm-3.0": "timesfm-3.0",
}
DEFAULT_DEVICE = "cpu"

# 上下文与预测步长的上界。模型自己的 global_context 是 15360 个点，
# 全用满会让单次前向的时间和激活内存都难以预测，所以默认收到 4096，
# 允许调到模型上限但不再往上放开。
MODEL_MAX_CONTEXT = 15360
DEFAULT_MAX_CONTEXT = 4096
DEFAULT_MAX_HORIZON = 512

# 单请求的序列条数。批内是顺序前向（见 engine），条数越多占用槽位越久。
DEFAULT_MAX_SERIES = 16

# 16 条 × 4096 点按 JSON 文本算约 0.5 MB，留一倍余量。
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

# 与 OCR、日志聚类同口径：目标主机核数与邻居共享，并发收敛到 2。
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_QUEUE_TIMEOUT_SECS = 60.0

DEFAULT_LOG_LEVEL = "info"


class ConfigError(Exception):
    """配置非法。启动阶段直接退出，不要带着半个配置开始服务。"""


@dataclass(frozen=True)
class Config:
    listen_addr: str
    models_dir: Path
    model_tier: str
    device: str
    infer_threads: int
    preload: bool
    verify_weights: bool
    max_context: int
    max_horizon: int
    max_series: int
    max_bytes: int
    max_concurrency: int
    queue_timeout_secs: float
    limit_concurrency: int
    log_level: str
    auth_token: str | None

    @property
    def model_dir(self) -> Path:
        """本档位权重的实际目录。"""
        return self.models_dir / TIER_SUBDIRS[self.model_tier]

    def profile(self) -> dict:
        """预测参数指纹：进 `/version`、`/models`，并被契约测试与 registry 校验。"""
        return {
            "tier": self.model_tier,
            "device": self.device,
            "max_context": self.max_context,
            "max_horizon": self.max_horizon,
        }

    @property
    def host_port(self) -> tuple[str, int]:
        return parse_listen_addr(self.listen_addr)

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        listen_addr = _text(env, "LISTEN_ADDR", DEFAULT_LISTEN_ADDR)
        # 早失败：端口写错不该等到 bind 的时候才报
        parse_listen_addr(listen_addr)

        models_dir = Path(_text(env, "MODELS_DIR", DEFAULT_MODELS_DIR))

        model_tier = _text(env, "MODEL_TIER", DEFAULT_MODEL_TIER)
        if model_tier not in TIER_SUBDIRS:
            raise ConfigError(
                f"MODEL_TIER='{model_tier}' 不是已知档位；"
                f"可用：{', '.join(sorted(TIER_SUBDIRS))}"
            )

        device = _text(env, "DEVICE", DEFAULT_DEVICE).lower()
        if device not in ("cpu", "cuda"):
            raise ConfigError(f"DEVICE='{device}' 只支持 cpu 或 cuda")

        infer_threads = _int(env, "INFER_THREADS", _default_threads())
        if infer_threads < 1:
            raise ConfigError("INFER_THREADS 必须 >= 1")

        preload = _bool(env, "PRELOAD", True)
        verify_weights = _bool(env, "VERIFY_WEIGHTS", True)

        max_context = _int(env, "MAX_CONTEXT", DEFAULT_MAX_CONTEXT)
        if not 1 <= max_context <= MODEL_MAX_CONTEXT:
            raise ConfigError(
                f"MAX_CONTEXT={max_context} 必须落在 1..{MODEL_MAX_CONTEXT}"
            )

        max_horizon = _int(env, "MAX_HORIZON", DEFAULT_MAX_HORIZON)
        if max_horizon < 1:
            raise ConfigError("MAX_HORIZON 必须 >= 1")

        max_series = _int(env, "MAX_SERIES", DEFAULT_MAX_SERIES)
        if max_series < 1:
            raise ConfigError("MAX_SERIES 必须 >= 1")

        max_bytes = _int(env, "MAX_BYTES", DEFAULT_MAX_BYTES)
        if max_bytes < 1:
            raise ConfigError("MAX_BYTES 必须 >= 1")

        max_concurrency = _int(env, "MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
        if max_concurrency < 1:
            raise ConfigError("MAX_CONCURRENCY 必须 >= 1")

        queue_timeout_secs = _float(
            env, "QUEUE_TIMEOUT_SECS", DEFAULT_QUEUE_TIMEOUT_SECS
        )
        if queue_timeout_secs <= 0:
            raise ConfigError("QUEUE_TIMEOUT_SECS 必须 > 0")

        limit_concurrency = _int(env, "LIMIT_CONCURRENCY", max(8, max_concurrency * 4))
        if limit_concurrency < 1:
            raise ConfigError("LIMIT_CONCURRENCY 必须 >= 1")

        log_level = _text(env, "LOG_LEVEL", DEFAULT_LOG_LEVEL).lower()
        if log_level not in ("critical", "error", "warning", "info", "debug", "trace"):
            raise ConfigError(f"LOG_LEVEL='{log_level}' 不是合法的日志级别")

        return Config(
            listen_addr=listen_addr,
            models_dir=models_dir,
            model_tier=model_tier,
            device=device,
            infer_threads=infer_threads,
            preload=preload,
            verify_weights=verify_weights,
            max_context=max_context,
            max_horizon=max_horizon,
            max_series=max_series,
            max_bytes=max_bytes,
            max_concurrency=max_concurrency,
            queue_timeout_secs=queue_timeout_secs,
            limit_concurrency=limit_concurrency,
            log_level=log_level,
            auth_token=_optional_text(env, "AUTH_TOKEN"),
        )


def _default_threads() -> int:
    """默认线程数。上限 4：这台机器上的 CPU 是与邻居共享的。"""
    return max(1, min(4, os.cpu_count() or 1))


def parse_listen_addr(value: str) -> tuple[str, int]:
    host, separator, raw_port = value.rpartition(":")
    if not separator or not host:
        raise ConfigError(f"LISTEN_ADDR='{value}' 必须是 host:port 形式")
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(f"LISTEN_ADDR='{value}' 的端口不是数字") from None
    if not 0 < port < 65536:
        raise ConfigError(f"LISTEN_ADDR='{value}' 的端口超出 0..65535")
    return host, port


def _text(env: Mapping[str, str], key: str, fallback: str) -> str:
    value = env.get(key, "").strip()
    return value or fallback


def _optional_text(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key, "").strip()
    return value or None


def _int(env: Mapping[str, str], key: str, fallback: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key}='{raw}' 不是整数") from None


def _float(env: Mapping[str, str], key: str, fallback: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{key}='{raw}' 不是数字") from None


def _bool(env: Mapping[str, str], key: str, fallback: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return fallback
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}='{raw}' 不是布尔值（1/0、true/false、yes/no、on/off）")
