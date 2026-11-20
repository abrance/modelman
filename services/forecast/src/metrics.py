"""Prometheus 文本暴露，零外部依赖。

口径与 OCR、日志聚类、判定服务一致：请求计数与延迟直方图带端点标签。
这里服务特有的量是预测规模（序列条数、上下文点数、预测步长）与常驻内存——
内存是这个服务的主要成本（实测加载峰值 2.8 GB），把它报出来，
容量问题才不用进容器里猜。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

# 一次预测在 16 线程上是秒级（实测 512 点上下文、48 步长 1.3 s），
# 长上下文+长步长会到十几秒，所以桶从 10 ms 起、最慢给到 120 s，
# 用来发现"某次请求把槽位堵死"。
DURATION_BUCKETS: tuple[float, ...] = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)

METRIC_PREFIX = "forecast"


class _Histogram:
    __slots__ = ("buckets", "count", "sum")

    def __init__(self) -> None:
        self.buckets = [0] * len(DURATION_BUCKETS)
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        for index, bound in enumerate(DURATION_BUCKETS):
            if value <= bound:
                self.buckets[index] += 1
        self.sum += value
        self.count += 1


class Metrics:
    """进程内指标。所有写操作都在一把小锁里，读时（render）也加锁取快照。"""

    def __init__(self, version: str, commit: str) -> None:
        self._lock = threading.Lock()
        self._version = version
        self._commit = commit
        self._in_flight = 0
        self._requests: dict[tuple[str, str], int] = defaultdict(int)
        self._durations: dict[str, _Histogram] = defaultdict(_Histogram)
        self._series = 0
        self._context_points = 0
        self._horizon_points = 0
        self._truncated = 0
        self._rejected: dict[str, int] = defaultdict(int)
        self._model_ready = False
        self._model_load_seconds = 0.0
        self._model_error: str | None = None

    # ── 写入 ────────────────────────────────────────────────────────────

    @contextmanager
    def in_flight(self) -> Iterator[None]:
        with self._lock:
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def record_success(
        self,
        endpoint: str,
        seconds: float,
        series: int = 0,
        context_points: int = 0,
        horizon: int = 0,
    ) -> None:
        with self._lock:
            self._requests[(endpoint, "ok")] += 1
            self._durations[endpoint].observe(seconds)
            self._series += series
            self._context_points += context_points
            self._horizon_points += series * horizon

    def record_failure(self, endpoint: str, reason: str) -> None:
        with self._lock:
            self._requests[(endpoint, reason)] += 1

    def record_truncated(self) -> None:
        """请求被上限截断的次数（上下文截到 MAX_CONTEXT 一类）。"""
        with self._lock:
            self._truncated += 1

    def record_rejected(self, reason: str) -> None:
        """没拿到并发槽位而被拒的次数。"""
        with self._lock:
            self._rejected[reason] += 1

    def set_model(self, ready: bool, load_seconds: float, error: str | None) -> None:
        with self._lock:
            self._model_ready = ready
            self._model_load_seconds = load_seconds
            self._model_error = error

    # ── 渲染 ────────────────────────────────────────────────────────────

    def render(self, uptime_secs: float) -> str:
        with self._lock:
            in_flight = self._in_flight
            requests = dict(self._requests)
            durations = {
                endpoint: (list(hist.buckets), hist.count, hist.sum)
                for endpoint, hist in self._durations.items()
            }
            series = self._series
            context_points = self._context_points
            horizon_points = self._horizon_points
            truncated = self._truncated
            rejected = dict(self._rejected)
            model_ready = self._model_ready
            model_load_seconds = self._model_load_seconds
            model_error = self._model_error

        out: list[str] = []
        out.append(f"# HELP {METRIC_PREFIX}_up 进程存活（1 = 在跑）")
        out.append(f"# TYPE {METRIC_PREFIX}_up gauge")
        out.append(f"{METRIC_PREFIX}_up 1")

        out.append(f"# HELP {METRIC_PREFIX}_uptime_seconds 进程运行时长")
        out.append(f"# TYPE {METRIC_PREFIX}_uptime_seconds gauge")
        out.append(f"{METRIC_PREFIX}_uptime_seconds {uptime_secs:.3f}")

        out.append(f"# HELP {METRIC_PREFIX}_build_info 构建信息（值为 1）")
        out.append(f"# TYPE {METRIC_PREFIX}_build_info gauge")
        out.append(
            f'{METRIC_PREFIX}_build_info{{version="{_escape(self._version)}",'
            f'commit="{_escape(self._commit)}"}} 1'
        )

        out.append(f"# HELP {METRIC_PREFIX}_model_ready 权重已常驻（1 = 可接流量）")
        out.append(f"# TYPE {METRIC_PREFIX}_model_ready gauge")
        out.append(f"{METRIC_PREFIX}_model_ready {1 if model_ready else 0}")

        out.append(f"# HELP {METRIC_PREFIX}_model_load_seconds 上一次权重加载耗时")
        out.append(f"# TYPE {METRIC_PREFIX}_model_load_seconds gauge")
        out.append(f"{METRIC_PREFIX}_model_load_seconds {model_load_seconds:.3f}")

        if model_error:
            out.append(f"# HELP {METRIC_PREFIX}_model_error 加载失败原因（值为 1 表示存在）")
            out.append(f"# TYPE {METRIC_PREFIX}_model_error gauge")
            out.append(
                f'{METRIC_PREFIX}_model_error{{reason="{_escape(_clip(model_error))}"}} 1'
            )

        out.append(f"# HELP {METRIC_PREFIX}_in_flight 正在处理的请求数")
        out.append(f"# TYPE {METRIC_PREFIX}_in_flight gauge")
        out.append(f"{METRIC_PREFIX}_in_flight {in_flight}")

        out.append(f"# HELP {METRIC_PREFIX}_requests_total 请求计数")
        out.append(f"# TYPE {METRIC_PREFIX}_requests_total counter")
        for (endpoint, status), count in sorted(requests.items()):
            out.append(
                f'{METRIC_PREFIX}_requests_total{{endpoint="{_escape(endpoint)}",'
                f'status="{_escape(status)}"}} {count}'
            )

        out.append(f"# HELP {METRIC_PREFIX}_request_duration_seconds 请求耗时")
        out.append(f"# TYPE {METRIC_PREFIX}_request_duration_seconds histogram")
        for endpoint in sorted(durations):
            buckets, count, total = durations[endpoint]
            for bound, value in zip(DURATION_BUCKETS, buckets):
                out.append(
                    f'{METRIC_PREFIX}_request_duration_seconds_bucket'
                    f'{{endpoint="{_escape(endpoint)}",le="{bound}"}} {value}'
                )
            out.append(
                f'{METRIC_PREFIX}_request_duration_seconds_bucket'
                f'{{endpoint="{_escape(endpoint)}",le="+Inf"}} {count}'
            )
            out.append(
                f'{METRIC_PREFIX}_request_duration_seconds_sum'
                f'{{endpoint="{_escape(endpoint)}"}} {total:.6f}'
            )
            out.append(
                f'{METRIC_PREFIX}_request_duration_seconds_count'
                f'{{endpoint="{_escape(endpoint)}"}} {count}'
            )

        out.append(f"# HELP {METRIC_PREFIX}_series_total 累计预测的序列条数")
        out.append(f"# TYPE {METRIC_PREFIX}_series_total counter")
        out.append(f"{METRIC_PREFIX}_series_total {series}")

        out.append(f"# HELP {METRIC_PREFIX}_context_points_total 累计吃进去的上下文点数")
        out.append(f"# TYPE {METRIC_PREFIX}_context_points_total counter")
        out.append(f"{METRIC_PREFIX}_context_points_total {context_points}")

        out.append(f"# HELP {METRIC_PREFIX}_horizon_points_total 累计产出的预测点数")
        out.append(f"# TYPE {METRIC_PREFIX}_horizon_points_total counter")
        out.append(f"{METRIC_PREFIX}_horizon_points_total {horizon_points}")

        out.append(f"# HELP {METRIC_PREFIX}_truncated_total 输入被上限截断的次数")
        out.append(f"# TYPE {METRIC_PREFIX}_truncated_total counter")
        out.append(f"{METRIC_PREFIX}_truncated_total {truncated}")

        out.append(f"# HELP {METRIC_PREFIX}_rejected_total 没拿到并发槽位而被拒的次数")
        out.append(f"# TYPE {METRIC_PREFIX}_rejected_total counter")
        for reason, count in sorted(rejected.items()):
            out.append(
                f'{METRIC_PREFIX}_rejected_total{{reason="{_escape(reason)}"}} {count}'
            )

        return "\n".join(out) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _clip(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
