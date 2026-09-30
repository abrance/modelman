"""预测引擎：权重生命周期、并发上界、TimesFM 3.0 调用。

三条硬性要求（与 OCR、日志聚类、判定服务同源）：

1. **推理放阻塞线程。** TimesFM 是同步的 CPU 计算，FastAPI 的同步 `def` 端点本来
   就跑在线程池里，所以不要写成 `async def` 直接算——那会占住事件循环。
2. **并发有上界。** 云主机 CPU 与邻居共享，一次预测要吃满几个核，请求堆积比快速
   失败更糟；用一个信号量把同时进入模型的请求数压在上界内，等不到就 503。
3. **加载失败不钉死进程。** 权重缺失、sha256 不符时进程照常启动，把原因暴露在
   `/models` 与 `/readyz` 上，业务端点一律 503；进程活着才有机会用 HTTP 去看原因。

锁只有一把，因为只有一个模型实例：锁粒度已经是"引擎级"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import Config
from .metrics import Metrics

logger = logging.getLogger(__name__)

# 构建期写下的权重摘要清单，`models/<tier>/sha256.json`。
DIGEST_MANIFEST = "sha256.json"

# 清单自带的元数据键，不是文件。
_MANIFEST_META = frozenset({"revision"})


class Overloaded(Exception):
    """在超时时间内没拿到并发槽位。"""


class ModelUnavailable(Exception):
    """权重不可用，业务操作一律拒绝。"""


class InvalidRequest(Exception):
    """请求不合法（条数、步长、序列形状）。折算成 400。"""


@dataclass(frozen=True)
class ForecastItem:
    id: str | None
    point: np.ndarray
    quantiles: dict[str, np.ndarray]


@dataclass(frozen=True)
class ForecastOutcome:
    model: str
    horizon: int
    levels: list[float]
    items: list[ForecastItem]
    context_lengths: list[int]
    elapsed_ms: float
    truncated: bool = False


@dataclass
class _ModelState:
    agent: object | None = None
    load_error: str | None = None
    load_seconds: float = 0.0
    digest_verified: bool = False
    digests: dict[str, str] = field(default_factory=dict)
    weights_bytes: int = 0
    revision: str | None = None
    levels: list[float] = field(default_factory=list)
    global_context: int = 0


class ForecastEngine:
    """持有唯一那套权重，并把它暴露成一个预测操作。"""

    def __init__(self, config: Config, metrics: Metrics) -> None:
        self._config = config
        self._metrics = metrics
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(config.max_concurrency)
        self._state = _ModelState()
        self._load_lock = threading.Lock()

        self._configure_threads()
        self._read_manifest()

        if config.preload:
            self.load()
        else:
            logger.info("preload disabled; weights load on first request")

    # ── 权重生命周期 ────────────────────────────────────────────────────

    def _configure_threads(self) -> None:
        """限制 torch 的 intra-op 线程数。

        必须在加载权重之前设。torch 由 timesfm 带进来，这里做顶层 import 是
        可接受的（镜像里必然有），但把异常收成加载错误，方便 /models 报出来。
        """
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - 依赖缺失由 /readyz 暴露
            self._state.load_error = f"torch 不可用：{exc}"
            return
        torch.set_num_threads(self._config.infer_threads)
        logger.info("torch intra-op threads=%d", torch.get_num_threads())

    def _read_manifest(self) -> None:
        """读构建期写下的摘要清单。清单缺失不是致命错误，但要在 /models 上说明。"""
        path = self._config.model_dir / DIGEST_MANIFEST
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("digest manifest not found at %s", path)
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("digest manifest unreadable: %s", exc)
            self._state.load_error = f"{DIGEST_MANIFEST} 不可读：{exc}"
            return
        if not isinstance(payload, dict):
            self._state.load_error = f"{DIGEST_MANIFEST} 不是 JSON 对象"
            return
        self._state.digests = {str(key): str(value) for key, value in payload.items()}
        self._state.revision = self._state.digests.get("revision")

    def load(self) -> bool:
        """加载权重。可重入：已就绪时是空操作，失败时保留原因不反复重试。"""
        with self._load_lock:
            if self._state.agent is not None:
                return True

            started = time.perf_counter()
            try:
                problem = self._verify_weights()
                if problem:
                    raise RuntimeError(problem)
                agent = self._build_agent()
            except Exception as exc:  # noqa: BLE001 - 加载期任何异常都降级，不要拖垮进程
                self._state.agent = None
                self._state.load_error = f"权重加载失败：{exc}"
                self._state.load_seconds = time.perf_counter() - started
                logger.exception("model load failed")
                self._metrics.set_model(
                    False, self._state.load_seconds, self._state.load_error
                )
                return False

            self._state.agent = agent
            self._state.load_error = None
            self._state.load_seconds = time.perf_counter() - started
            self._state.weights_bytes = _dir_size(self._config.model_dir)
            self._state.levels = [float(level) for level in agent.config.quantiles]
            self._state.global_context = int(agent.global_context)
            self._metrics.set_model(True, self._state.load_seconds, None)
            logger.info(
                "model ready: tier=%s device=%s load_seconds=%.2f weights_bytes=%d "
                "global_context=%d quantiles=%s digests=%d",
                self._config.model_tier,
                self._config.device,
                self._state.load_seconds,
                self._state.weights_bytes,
                self._state.global_context,
                self._state.levels,
                len(self._state.digests),
            )
            return True

    def _verify_weights(self) -> str | None:
        """按清单逐文件核 sha256。返回问题描述，没问题返回 None。

        构建期已经核过一遍，这里再核是为了挡住"权重文件在盘上被换掉/截断"——
        模型加载对损坏的 safetensors 不一定报错，可能静默给出错误结果。
        """
        if not self._config.verify_weights:
            return None
        files = {
            name: digest
            for name, digest in self._state.digests.items()
            if name not in _MANIFEST_META
        }
        if not files:
            return "摘要清单里没有文件，拒绝加载（VERIFY_WEIGHTS 打开时必须有清单）"

        problems = []
        for name, expected in files.items():
            target = self._config.model_dir / name
            if not target.is_file():
                problems.append(f"{name}: 缺失")
                continue
            actual = sha256_file(target)
            if actual.lower() != str(expected).lower():
                problems.append(f"{name}: 期望 {expected}，实际 {actual}")
        if problems:
            return "；".join(problems)

        self._state.digest_verified = True
        return None

    def _build_agent(self):
        from timesfm3 import TimesFM3Forecaster

        model_dir = str(self._config.model_dir)
        # 权重随镜像交付（构建期取回并校验），运行期不联网：
        # 本地目录一定要显式声明 local_files_only，否则 hub 客户端可能在
        # 权重文件缺失时退化成"去 HuggingFace 找一份"，那在部署主机上必然超时。
        local = os.path.isdir(model_dir)
        return TimesFM3Forecaster.from_pretrained(
            model_dir,
            device=self._config.device,
            local_files_only=local,
        )

    # ── 状态问答 ────────────────────────────────────────────────────────

    @property
    def model_tier(self) -> str:
        return self._config.model_tier

    @property
    def config(self) -> Config:
        """生效配置。只读暴露，便于测试与自检脚本按同一份配置构造请求。"""
        return self._config

    @property
    def load_error(self) -> str | None:
        return self._state.load_error

    @property
    def levels(self) -> list[float]:
        """模型自带的分位水平。权重没加载时是空列表。"""
        return list(self._state.levels)

    def is_ready(self) -> bool:
        """就绪 = 权重已常驻。未就绪时进程仍在跑，但 /readyz 返回 503。"""
        return self._state.agent is not None

    def model_metadata(self) -> dict:
        return {
            "tier": self._config.model_tier,
            "model_dir": str(self._config.model_dir),
            "device": self._config.device,
            "infer_threads": self._config.infer_threads,
            "weights_bytes": self._state.weights_bytes,
            "digest_verified": self._state.digest_verified,
            "digests": len(self._state.digests),
            "revision": self._state.revision,
            "load_seconds": round(self._state.load_seconds, 3),
            "global_context": self._state.global_context,
            "quantiles": self._state.levels,
        }

    # ── 业务操作 ────────────────────────────────────────────────────────

    def forecast(
        self,
        series: list[tuple[str | None, np.ndarray]],
        horizon: int,
        levels: list[float],
    ) -> ForecastOutcome:
        self._require_ready()
        indices = self._level_indices(levels)

        contexts = [values for _id, values in series]
        ids = [ts_id for ts_id, _values in series]
        lengths = [int(values.shape[-1]) for values in contexts]

        with self._slot():
            with self._lock:
                started = time.perf_counter()
                results = list(
                    self._state.agent.predict_batch(
                        contexts=contexts,
                        horizon=horizon,
                        ts_ids=ids,
                        return_quantiles=True,
                    )
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0

        items = []
        for result in results:
            quantiles = np.asarray(result.quantiles)
            # 单变量：(horizon, 分位数)；多变量：(变量, horizon, 分位数)。
            # 统一按"最后一个轴是分位"取值。
            items.append(
                ForecastItem(
                    id=result.ts_id,
                    point=np.asarray(result.forecast),
                    quantiles={
                        level_key(levels[index]): np.take(
                            quantiles, column, axis=-1
                        )
                        for index, column in enumerate(indices)
                    },
                )
            )

        return ForecastOutcome(
            model=self._config.model_tier,
            horizon=horizon,
            levels=list(levels),
            items=items,
            context_lengths=lengths,
            elapsed_ms=elapsed_ms,
            truncated=any(
                int(values.shape[-1]) >= self._config.max_context
                for _id, values in series
            ),
        )

    def _level_indices(self, levels: list[float]) -> list[int]:
        available = self._state.levels
        indices = []
        for level in levels:
            try:
                indices.append(available.index(level))
            except ValueError:
                raise InvalidRequest(
                    f"分位 {level} 不在模型自带的分位里；"
                    f"可用：{', '.join(level_key(item) for item in available)}"
                ) from None
        return indices

    def _require_ready(self) -> None:
        if self._state.agent is None:
            self.load()
        if self._state.agent is None:
            raise ModelUnavailable(self._state.load_error or "权重未就绪")

    @contextmanager
    def _slot(self) -> Iterator[None]:
        if not self._slots.acquire(timeout=self._config.queue_timeout_secs):
            self._metrics.record_rejected("overloaded")
            raise Overloaded(
                f"服务忙：等待 {self._config.queue_timeout_secs:g} 秒仍没有空闲的推理槽位"
            )
        try:
            yield
        finally:
            self._slots.release()


# ─── 请求校验与整形 ─────────────────────────────────────────────────────────


def prepare_series(series, config: Config) -> tuple[list[tuple[str | None, np.ndarray]], bool]:
    """把请求里的 series 折成 numpy，并做形状校验。

    返回 `(序列列表, 是否截断)`。上下文超过 `MAX_CONTEXT` 时**取最后一段**而不是
    拒绝：时序预测里越近的点越重要，截尾比报错有用；截断这件事由响应里的
    `truncated` 与指标里的计数器说明。
    """
    if not series:
        raise InvalidRequest("series 不能为空")
    if len(series) > config.max_series:
        raise InvalidRequest(
            f"序列条数 {len(series)} 超过 MAX_SERIES={config.max_series}"
        )

    kind: str | None = None
    variates: int | None = None
    prepared: list[tuple[str | None, np.ndarray]] = []
    truncated = False

    for index, item in enumerate(series):
        values = item.values
        # pydantic 已经拦了空 values，这里再拦一次：prepare_series 是公开的整形入口，
        # 不该假设调用方一定经过 pydantic。
        if not values:
            raise InvalidRequest(f"series[{index}] 至少要有一个点")
        multivariate = isinstance(values[0], list)
        if kind is None:
            kind = "multi" if multivariate else "single"
        elif kind != ("multi" if multivariate else "single"):
            raise InvalidRequest(
                f"series[{index}] 的 values 形状与 series[0] 不一致："
                "要么都是单变量（数字数组），要么都是多变量（数组的数组）"
            )

        if multivariate:
            width = len(values[0])
            if width == 0:
                raise InvalidRequest(f"series[{index}] 的每个变量至少要有一个点")
            for row_index, row in enumerate(values):
                if len(row) != width:
                    raise InvalidRequest(
                        f"series[{index}] 第 {row_index} 个变量有 {len(row)} 个点，"
                        f"与第一个变量的 {width} 个点不一致"
                    )
            if variates is None:
                variates = len(values)
            elif variates != len(values):
                raise InvalidRequest(
                    f"series[{index}] 有 {len(values)} 个变量，"
                    f"与 series[0] 的 {variates} 个不一致"
                )
            array = np.asarray(values, dtype=np.float32)
        else:
            array = np.asarray(values, dtype=np.float32).reshape(-1)

        if array.shape[-1] == 0:
            raise InvalidRequest(f"series[{index}] 至少要有一个点")
        if array.shape[-1] == 0:
            raise InvalidRequest(f"series[{index}] 至少要有一个点")
        if array.shape[-1] > config.max_context:
            array = array[..., -config.max_context :]
            truncated = True
        if not np.isfinite(array).all():
            raise InvalidRequest(f"series[{index}] 含 NaN/Inf")

        prepared.append((item.id, array))

    return prepared, truncated


def validate_request(request, config: Config) -> None:
    """业务端点的前置校验。分位是否可用要等权重加载后才能判，放在引擎里。"""
    if request.horizon > config.max_horizon:
        raise InvalidRequest(
            f"horizon={request.horizon} 超过 MAX_HORIZON={config.max_horizon}"
        )
    if request.quantiles is not None:
        if not request.quantiles:
            raise InvalidRequest("quantiles 不能是空列表（要么不传，要么至少给一个分位）")
        for level in request.quantiles:
            if not 0.0 < level < 1.0:
                raise InvalidRequest(f"分位 {level} 必须落在 0 与 1 之间（不含端点）")


def resolve_levels(request, available: list[float]) -> list[float]:
    """请求里没给分位就用模型自带的全部。"""
    if request.quantiles is None:
        return list(available)
    return sorted({float(level) for level in request.quantiles})


def level_key(level: float) -> str:
    """分位水平的 JSON 键。用 repr 去掉浮点噪声（0.1 而不是 0.10000000000000001）。"""
    return repr(float(level))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_manifest(model_dir: Path, files: list[str], revision: str | None) -> dict:
    """构建期生成摘要清单：逐文件 sha256 + 权重版本。"""
    manifest = {name: sha256_file(model_dir / name) for name in files}
    if revision:
        manifest["revision"] = revision
    return manifest


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(Path(root) / name)
            except OSError:
                continue
    return total
