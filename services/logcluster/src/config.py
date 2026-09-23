"""环境变量解析与聚类档位。这是唯一读环境变量的模块。

其余模块只接受 `Config`，测试可以直接构造它而不用改进程环境。
默认值保证在开发机上开箱能跑：`STATE_DIR` 缺省落在服务目录下的 `.state/`。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .masks import available_rules, resolve_rules

DEFAULT_LISTEN_ADDR = "0.0.0.0:8080"
DEFAULT_STATE_DIR = ".state"

# 单档常驻，没有多档位可选，所以档位 id 固定。
PROFILE_ID = "default"

# Drain3 默认参数。sim_th 越低越容易合并，0.4 是上游默认值，
# 与 NAS 上正在跑的实例保持一致，替换时输出不会跳变。
DEFAULT_SIM_TH = 0.4
DEFAULT_DEPTH = 4
DEFAULT_MAX_CHILDREN = 100
DEFAULT_PARAMETRIZE_NUMERIC = True
DEFAULT_MASK_RULES: tuple[str, ...] = ()
DEFAULT_SNAPSHOT_COMPRESS = True
DEFAULT_SNAPSHOT_INTERVAL_MINUTES = 5

# 与 OCR 服务同口径：目标主机 4 核且与邻居共享，并发默认收敛到 2。
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_QUEUE_TIMEOUT_SECS = 30.0

# 输入上限。日志是纯文本，比图片小得多，但一条超长行会让正则与分词
# 变得很贵，所以行数与单行长度都要设上界。
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_LINE_CHARS = 8192
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

DEFAULT_LOG_LEVEL = "info"


class ConfigError(Exception):
    """配置非法。启动阶段直接退出，不要带着半个配置开始服务。"""


@dataclass(frozen=True)
class Config:
    listen_addr: str
    state_dir: Path
    sim_th: float
    depth: int
    max_children: int
    mask_rules: tuple[str, ...]
    parametrize_numeric: bool
    snapshot_compress: bool
    snapshot_interval_minutes: int
    max_concurrency: int
    queue_timeout_secs: float
    max_lines: int
    max_line_chars: int
    max_bytes: int
    limit_concurrency: int
    log_level: str
    auth_token: str | None

    @property
    def profile_id(self) -> str:
        return PROFILE_ID

    def profile(self) -> dict:
        """聚类参数指纹，写进状态文件并参与兼容性校验。

        字段顺序固定、值都可 JSON 序列化，便于在测试里逐字比较。
        """
        return {
            "id": PROFILE_ID,
            "sim_th": self.sim_th,
            "depth": self.depth,
            "max_children": self.max_children,
            "mask_rules": list(self.mask_rules),
            "parametrize_numeric": self.parametrize_numeric,
            "snapshot_compress": self.snapshot_compress,
        }

    @property
    def masking_instructions(self) -> list:
        return resolve_rules(self.mask_rules)

    @property
    def host_port(self) -> tuple[str, int]:
        return parse_listen_addr(self.listen_addr)

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        listen_addr = _text(env, "LISTEN_ADDR", DEFAULT_LISTEN_ADDR)
        # 早失败：端口写错不该等到 bind 的时候才报
        parse_listen_addr(listen_addr)

        state_dir = Path(_text(env, "STATE_DIR", DEFAULT_STATE_DIR))

        sim_th = _float(env, "SIM_TH", DEFAULT_SIM_TH)
        if not 0.0 < sim_th <= 1.0:
            raise ConfigError(f"SIM_TH={sim_th} 必须落在 0.0 < SIM_TH <= 1.0")

        depth = _int(env, "DEPTH", DEFAULT_DEPTH)
        if depth < 3:
            raise ConfigError(f"DEPTH={depth} 必须 >= 3（drain3 的硬性要求）")

        max_children = _int(env, "MAX_CHILDREN", DEFAULT_MAX_CHILDREN)
        if max_children < 2:
            raise ConfigError(f"MAX_CHILDREN={max_children} 必须 >= 2")

        raw_rules = _list(env, "MASK_RULES", DEFAULT_MASK_RULES)
        unknown = [name for name in raw_rules if name not in available_rules()]
        if unknown:
            raise ConfigError(
                f"MASK_RULES 含未知规则 {', '.join(unknown)}；"
                f"可用：{', '.join(available_rules())} 或留空"
            )

        parametrize_numeric = _bool(
            env, "PARAMETRIZE_NUMERIC", DEFAULT_PARAMETRIZE_NUMERIC
        )
        snapshot_compress = _bool(env, "SNAPSHOT_COMPRESS", DEFAULT_SNAPSHOT_COMPRESS)

        snapshot_interval_minutes = _int(
            env, "SNAPSHOT_INTERVAL_MINUTES", DEFAULT_SNAPSHOT_INTERVAL_MINUTES
        )
        if snapshot_interval_minutes < 1:
            raise ConfigError(
                f"SNAPSHOT_INTERVAL_MINUTES={snapshot_interval_minutes} 必须 >= 1"
            )

        max_concurrency = _int(env, "MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
        if max_concurrency < 1:
            raise ConfigError("MAX_CONCURRENCY 必须 >= 1")

        queue_timeout_secs = _float(
            env, "QUEUE_TIMEOUT_SECS", DEFAULT_QUEUE_TIMEOUT_SECS
        )
        if queue_timeout_secs <= 0:
            raise ConfigError("QUEUE_TIMEOUT_SECS 必须 > 0")

        max_lines = _int(env, "MAX_LINES", DEFAULT_MAX_LINES)
        if max_lines < 1:
            raise ConfigError("MAX_LINES 必须 >= 1")

        max_line_chars = _int(env, "MAX_LINE_CHARS", DEFAULT_MAX_LINE_CHARS)
        if max_line_chars < 1:
            raise ConfigError("MAX_LINE_CHARS 必须 >= 1")

        max_bytes = _int(env, "MAX_BYTES", DEFAULT_MAX_BYTES)
        if max_bytes < 1:
            raise ConfigError("MAX_BYTES 必须 >= 1")

        # uvicorn 的并发上限。超出后 uvicorn 直接回 503，这样排队深度
        # 就有界，不会因为大量等待而把内存吃光。
        limit_concurrency = _int(env, "LIMIT_CONCURRENCY", max(8, max_concurrency * 4))
        if limit_concurrency < 1:
            raise ConfigError("LIMIT_CONCURRENCY 必须 >= 1")

        log_level = _text(env, "LOG_LEVEL", DEFAULT_LOG_LEVEL).lower()
        if log_level not in ("critical", "error", "warning", "info", "debug", "trace"):
            raise ConfigError(f"LOG_LEVEL='{log_level}' 不是合法的日志级别")

        return Config(
            listen_addr=listen_addr,
            state_dir=state_dir,
            sim_th=sim_th,
            depth=depth,
            max_children=max_children,
            mask_rules=tuple(raw_rules),
            parametrize_numeric=parametrize_numeric,
            snapshot_compress=snapshot_compress,
            snapshot_interval_minutes=snapshot_interval_minutes,
            max_concurrency=max_concurrency,
            queue_timeout_secs=queue_timeout_secs,
            max_lines=max_lines,
            max_line_chars=max_line_chars,
            max_bytes=max_bytes,
            limit_concurrency=limit_concurrency,
            log_level=log_level,
            auth_token=_optional_text(env, "AUTH_TOKEN"),
        )


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


def _list(env: Mapping[str, str], key: str, fallback: Sequence[str]) -> list[str]:
    """逗号分隔列表。显式设成空字符串表示"就是空的"，与"未设置"区分开。"""
    if key not in env:
        return list(fallback)
    raw = env[key]
    return [item.strip() for item in raw.split(",") if item.strip()]
