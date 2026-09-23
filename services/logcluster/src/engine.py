"""聚类引擎：状态生命周期、并发上界、drain3 调用。

两条硬性要求（与 OCR 服务同源）：

1. **推理放阻塞线程。** drain3 是同步的 CPU 计算，FastAPI 的同步 `def`
   端点本来就跑在线程池里，所以这里不需要再套一层线程，但不能写成
   `async def` 直接算——那会占住事件循环。
2. **并发有上界。** 云主机 CPU 与邻居共享，请求堆积比快速失败更糟，
   所以用一个信号量把同时进入 drain3 的请求数压在上界内，等不到就 503。

锁只有一把，因为只有一棵模板树：锁粒度已经是"引擎级"。
`match` 与 `list_clusters` 其实是只读的，与写共用一把锁是刻意的取舍——
读写分离要自己实现读写锁，收益（少量只读请求少等一会儿）小于出错风险。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from .config import Config
from .metrics import Metrics
from .state import AtomicStatePersistence, StateError

logger = logging.getLogger(__name__)


class Overloaded(Exception):
    """在超时时间内没拿到并发槽位。"""


class StateUnavailable(Exception):
    """状态文件不可用，业务操作一律拒绝。"""


@dataclass(frozen=True)
class ClusterOutcome:
    results: list[dict]
    cluster_count: int
    elapsed_ms: float


@dataclass(frozen=True)
class MatchOutcome:
    results: list[dict]
    elapsed_ms: float


class DrainEngine:
    """持有唯一那棵模板树，并把它暴露成三个操作。"""

    def __init__(self, config: Config, metrics: Metrics) -> None:
        self._config = config
        self._metrics = metrics
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(config.max_concurrency)
        self._load_error: str | None = None

        # load_state() 由 TemplateMiner 构造时调用。状态不兼容或损坏时
        # 不定死进程：那样只能看容器日志。改为降级——启动、把原因暴露在
        # /models 与 /readyz 上、业务端点一律 503，并且**不写状态文件**，
        # 这样旧状态不会被一次失败的启动覆盖掉。
        # 注意 StateError 是在任何 drain 字段被赋值前抛出的，所以下面
        # 重建的这棵空树不会带着半个旧状态。
        self._persistence = AtomicStatePersistence(
            path=config.state_dir / "drain_state.json",
            profile=config.profile(),
        )
        self._miner = self._build_miner(config, self._persistence)

        clusters, messages = self._counts()
        self._metrics.set_state(clusters, messages, self._persistence.loaded)
        logger.info(
            "engine ready: state_loaded=%s clusters=%d messages=%d profile=%s",
            self._persistence.loaded,
            clusters,
            messages,
            config.profile(),
        )

    def _build_miner(self, config: Config, persistence):
        """构造 miner。状态不可用时记下原因并退化成不落盘的空树。"""
        try:
            return TemplateMiner(
                persistence_handler=persistence,
                config=build_drain_config(config),
            )
        except StateError as exc:
            self._load_error = str(exc)
            logger.error("state unavailable, serving /readyz 503: %s", exc)
            return TemplateMiner(
                persistence_handler=None,
                config=build_drain_config(config),
            )

    # ── 状态问答 ────────────────────────────────────────────────────────

    @property
    def state_loaded(self) -> bool:
        """启动时是否载入了已有状态（而不是从空树开始）。"""
        return self._persistence.loaded

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def state_metadata(self) -> dict:
        return self._persistence.metadata()

    def is_ready(self) -> bool:
        """就绪 = 状态可用。状态不可用时进程仍在跑，但 /readyz 返回 503，
        部署链路会因此卡在健康门上，不会把坏状态推给调用方。"""
        return self._load_error is None

    def _require_ready(self) -> None:
        if self._load_error is not None:
            raise StateUnavailable(self._load_error)

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return self._counts()

    def _counts(self) -> tuple[int, int]:
        return len(
            self._miner.drain.clusters
        ), self._miner.drain.get_total_cluster_size()

    # ── 业务操作 ────────────────────────────────────────────────────────

    def cluster(self, lines: Sequence[str]) -> ClusterOutcome:
        """在线学习：逐行喂给 drain3，返回每行的簇与模板。"""
        self._require_ready()
        with self._slot():
            with self._lock:
                started = _now()
                results = []
                for line in lines:
                    result = self._miner.add_log_message(line)
                    template = result["template_mined"]
                    results.append(
                        {
                            "line": line,
                            "cluster_id": result["cluster_id"],
                            "template": template,
                            "change_type": result["change_type"],
                            "parameters": self._extract(template, line),
                        }
                    )
                clusters, messages = self._counts()
            elapsed_ms = _elapsed_ms(started)
        self._metrics.set_state(clusters, messages, self._persistence.loaded)
        return ClusterOutcome(
            results=results, cluster_count=clusters, elapsed_ms=elapsed_ms
        )

    def match(self, lines: Sequence[str]) -> MatchOutcome:
        """只读匹配：不新建簇，也不改已有模板。"""
        self._require_ready()
        with self._slot():
            with self._lock:
                started = _now()
                results = []
                for line in lines:
                    matched = self._miner.match(line)
                    if matched is None:
                        results.append(
                            {
                                "line": line,
                                "matched": False,
                                "cluster_id": None,
                                "template": None,
                            }
                        )
                    else:
                        results.append(
                            {
                                "line": line,
                                "matched": True,
                                "cluster_id": matched.cluster_id,
                                "template": matched.get_template(),
                            }
                        )
            elapsed_ms = _elapsed_ms(started)
        return MatchOutcome(results=results, elapsed_ms=elapsed_ms)

    def list_clusters(self) -> tuple[list[dict], int]:
        self._require_ready()
        with self._lock:
            clusters = [
                {
                    "cluster_id": cluster.cluster_id,
                    "template": cluster.get_template(),
                    "size": cluster.size,
                }
                for cluster in self._miner.drain.clusters
            ]
            total = self._miner.drain.get_total_cluster_size()
        clusters.sort(key=lambda item: item["cluster_id"])
        return clusters, total

    def save_state(self, reason: str) -> bool:
        """显式落盘。正常路径上 drain3 会在模板变化时自己调，
        这里是给进程退出用的一次收尾。"""
        if self._load_error is not None:
            # 状态文件已经有问题，别再覆盖它
            logger.warning("skip state save: %s", self._load_error)
            return False
        try:
            with self._lock:
                self._miner.save_state(reason)
        except StateError as exc:
            logger.error("state save failed: %s", exc)
            self._metrics.record_state_save(False)
            return False
        self._metrics.record_state_save(True)
        return True

    # ── 内部 ────────────────────────────────────────────────────────────

    def _extract(self, template: str, line: str) -> list[str] | None:
        """提取模板里的可变参数。

        传入的是**原始行**：drain3 在 exact_matching 模式下会用配置里的
        脱敏正则去匹配，所以不需要提前把行脱敏。匹配不上就返回 None
        （例如低相似度合并过来的行），不要把整条请求打成 500。
        """
        try:
            params = self._miner.extract_parameters(template, line, exact_matching=True)
        except Exception as exc:  # noqa: BLE001 - drain3 内部正则异常不该影响主流程
            logger.debug("parameter extraction failed: %s", exc)
            return None
        if params is None:
            return None
        return [param.value for param in params]

    @contextmanager
    def _slot(self) -> Iterator[None]:
        if not self._slots.acquire(timeout=self._config.queue_timeout_secs):
            self._metrics.record_rejected("overloaded")
            raise Overloaded(
                f"服务忙：等待 {self._config.queue_timeout_secs:g} 秒仍没有空闲的聚类槽位"
            )
        try:
            yield
        finally:
            self._slots.release()


def _now() -> float:
    return time.perf_counter()


def build_drain_config(config: Config) -> TemplateMinerConfig:
    """把服务配置翻译成 drain3 的配置。

    独立成模块级函数，测试可以直接拿它建一个纯 `TemplateMiner`，
    不必绕过 `DrainEngine` 去够内部字段。
    """
    drain_config = TemplateMinerConfig()
    drain_config.drain_sim_th = config.sim_th
    drain_config.drain_depth = config.depth
    drain_config.drain_max_children = config.max_children
    drain_config.parametrize_numeric_tokens = config.parametrize_numeric
    drain_config.masking_instructions = config.masking_instructions
    drain_config.mask_prefix = "<"
    drain_config.mask_suffix = ">"
    drain_config.snapshot_compress_state = config.snapshot_compress
    drain_config.snapshot_interval_minutes = config.snapshot_interval_minutes
    return drain_config


def _elapsed_ms(started: float) -> float:
    return (_now() - started) * 1000.0
