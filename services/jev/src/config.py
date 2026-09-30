"""环境变量解析。这是唯一读环境变量的模块。

其余模块只接受 `Config`，测试可以直接构造它而不用改进程环境。
默认值保证在开发机上开箱能跑：`MODELS_DIR` 缺省是服务目录下的 `models/`，
权重随镜像交付，运行期不联网。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LISTEN_ADDR = "0.0.0.0:8080"
DEFAULT_MODELS_DIR = "models"

# 唯一交付的档位。多档位会让 Router 在语言之间来回切 checkpoint，
# 换来双份常驻内存（见 docs/jev.md 的取舍），所以这里只认一个。
DEFAULT_MODEL_TIER = "multilingual"
TIER_SUBDIRS = {
    "english": "english",
    "multilingual": "multilingual",
    "typed-decisions": "typed-decisions",
}
DEFAULT_DEVICE = "cpu"

# Laya 单次前向的 token 预算。multilingual 默认 1024，上限 8192；
# 上限越高，长输入的激活与耗时越难预测，所以默认值保持上游默认。
DEFAULT_MAX_LEN = 1024
DEFAULT_HEAD_MAX_LEN = 256

# 输入上限。state 与选项都要过 tokenizer，超长输入会变成不可预测的
# 内存与时间开销，所以字符数、问题数、选项数三处都设上界。
DEFAULT_MAX_STATE_CHARS = 8000
DEFAULT_MAX_QUESTIONS = 32
# 上游 HTTP 服务的上限是 100（超出回 413），这里取同值。
DEFAULT_MAX_OPTIONS = 100
DEFAULT_MAX_BYTES = 1024 * 1024

# 与 OCR、日志聚类同口径：目标主机核数与邻居共享，并发收敛到 2。
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_QUEUE_TIMEOUT_SECS = 30.0

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
    max_len: int
    head_max_len: int
    max_state_chars: int
    max_questions: int
    max_options: int
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
        """判定参数指纹：进 `/version`、`/models`，并被契约测试与 registry 校验。

        字段顺序固定、值都可 JSON 序列化，便于逐字比较。
        """
        return {
            "tier": self.model_tier,
            "device": self.device,
            "max_len": self.max_len,
            "head_max_len": self.head_max_len,
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

        max_len = _int(env, "MAX_LEN", DEFAULT_MAX_LEN)
        if not 8 <= max_len <= 8192:
            raise ConfigError(f"MAX_LEN={max_len} 必须落在 8..8192")

        head_max_len = _int(env, "HEAD_MAX_LEN", DEFAULT_HEAD_MAX_LEN)
        if head_max_len < 8:
            raise ConfigError(f"HEAD_MAX_LEN={head_max_len} 必须 >= 8")
        if head_max_len > max_len:
            raise ConfigError(
                f"HEAD_MAX_LEN={head_max_len} 不能大于 MAX_LEN={max_len}"
            )

        max_state_chars = _int(env, "MAX_STATE_CHARS", DEFAULT_MAX_STATE_CHARS)
        if max_state_chars < 1:
            raise ConfigError("MAX_STATE_CHARS 必须 >= 1")

        max_questions = _int(env, "MAX_QUESTIONS", DEFAULT_MAX_QUESTIONS)
        if max_questions < 1:
            raise ConfigError("MAX_QUESTIONS 必须 >= 1")

        max_options = _int(env, "MAX_OPTIONS", DEFAULT_MAX_OPTIONS)
        if max_options < 2:
            raise ConfigError("MAX_OPTIONS 必须 >= 2")

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
            max_len=max_len,
            head_max_len=head_max_len,
            max_state_chars=max_state_chars,
            max_questions=max_questions,
            max_options=max_options,
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
