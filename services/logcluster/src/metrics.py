"""Prometheus 文本暴露，零外部依赖。

指标口径与 OCR 服务保持一致：请求计数与延迟直方图带端点标签，
另外单独暴露几个只有这个服务才有的量（模板数、消息总数、状态快照结果）。
自己拼文本而不是引入 prometheus_client，是为了让输出稳定、依赖面最小。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

# 请求耗时直方图的桶上界，单位秒。日志聚类是毫秒级的纯 CPU 计算，
# 最慢的桶给到 30 秒，用来发现"某次请求把队列堵住"这类退化。
DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
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
)

METRIC_PREFIX = "logcluster"


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
        self._lines: dict[str, int] = defaultdict(int)
        self._rejected: dict[str, int] = defaultdict(int)
        self._state_saves: dict[str, int] = defaultdict(int)
        self._clusters = 0
        self._messages = 0
        self._state_loaded = 0

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

    def record_success(self, endpoint: str, seconds: float, lines: int) -> None:
        with self._lock:
            self._requests[(endpoint, "ok")] += 1
            self._durations[endpoint].observe(seconds)
            self._lines[endpoint] += lines

    def record_failure(self, endpoint: str, status: str) -> None:
        with self._lock:
            self._requests[(endpoint, status)] += 1

    def record_rejected(self, reason: str) -> None:
        with self._lock:
            self._rejected[reason] += 1

    def record_state_save(self, ok: bool) -> None:
        with self._lock:
            self._state_saves["ok" if ok else "failed"] += 1

    def set_state(self, clusters: int, messages: int, loaded: bool) -> None:
        with self._lock:
            self._clusters = clusters
            self._messages = messages
            self._state_loaded = 1 if loaded else 0

    # ── 暴露 ────────────────────────────────────────────────────────────

    def render(self, uptime_secs: float) -> str:
        with self._lock:
            in_flight = self._in_flight
            requests = dict(self._requests)
            durations = {
                key: (list(value.buckets), value.sum, value.count)
                for key, value in self._durations.items()
            }
            lines = dict(self._lines)
            rejected = dict(self._rejected)
            saves = dict(self._state_saves)
            clusters, messages, state_loaded = (
                self._clusters,
                self._messages,
                self._state_loaded,
            )

        out: list[str] = []
        prefix = METRIC_PREFIX

        out.append(f"# HELP {prefix}_up 进程正在服务时为 1。\n")
        out.append(f"# TYPE {prefix}_up gauge\n")
        out.append(f"{prefix}_up 1\n")

        out.append(f"# HELP {prefix}_build_info 版本与提交，值恒为 1。\n")
        out.append(f"# TYPE {prefix}_build_info gauge\n")
        out.append(
            f'{prefix}_build_info{{version="{_escape(self._version)}",'
            f'commit="{_escape(self._commit)}"}} 1\n'
        )

        out.append(f"# HELP {prefix}_uptime_seconds 进程已运行秒数。\n")
        out.append(f"# TYPE {prefix}_uptime_seconds gauge\n")
        out.append(f"{prefix}_uptime_seconds {uptime_secs:.3f}\n")

        out.append(f"# HELP {prefix}_requests_in_flight 正在处理中的请求数。\n")
        out.append(f"# TYPE {prefix}_requests_in_flight gauge\n")
        out.append(f"{prefix}_requests_in_flight {in_flight}\n")

        out.append(f"# HELP {prefix}_state_loaded 状态文件已载入时为 1。\n")
        out.append(f"# TYPE {prefix}_state_loaded gauge\n")
        out.append(f"{prefix}_state_loaded {state_loaded}\n")

        out.append(f"# HELP {prefix}_clusters 当前模板数。\n")
        out.append(f"# TYPE {prefix}_clusters gauge\n")
        out.append(f"{prefix}_clusters {clusters}\n")

        out.append(f"# HELP {prefix}_cluster_messages 已学习的日志行总数。\n")
        out.append(f"# TYPE {prefix}_cluster_messages gauge\n")
        out.append(f"{prefix}_cluster_messages {messages}\n")

        out.append(f"# HELP {prefix}_requests_total 已完成的请求数。\n")
        out.append(f"# TYPE {prefix}_requests_total counter\n")
        for (endpoint, status), value in sorted(requests.items()):
            out.append(
                f'{prefix}_requests_total{{endpoint="{_escape(endpoint)}",'
                f'status="{_escape(status)}"}} {value}\n'
            )

        out.append(f"# HELP {prefix}_request_duration_seconds 请求耗时。\n")
        out.append(f"# TYPE {prefix}_request_duration_seconds histogram\n")
        for endpoint, (buckets, total, count) in sorted(durations.items()):
            cumulative = 0
            for index, bound in enumerate(DURATION_BUCKETS):
                cumulative += buckets[index]
                out.append(
                    f'{prefix}_request_duration_seconds_bucket{{endpoint="{_escape(endpoint)}",'
                    f'le="{bound}"}} {cumulative}\n'
                )
            out.append(
                f'{prefix}_request_duration_seconds_bucket{{endpoint="{_escape(endpoint)}",'
                f'le="+Inf"}} {count}\n'
            )
            out.append(
                f'{prefix}_request_duration_seconds_sum{{endpoint="{_escape(endpoint)}"}} {total:.6f}\n'
            )
            out.append(
                f'{prefix}_request_duration_seconds_count{{endpoint="{_escape(endpoint)}"}} {count}\n'
            )

        out.append(f"# HELP {prefix}_lines_total 已处理的日志行数。\n")
        out.append(f"# TYPE {prefix}_lines_total counter\n")
        for endpoint, value in sorted(lines.items()):
            out.append(
                f'{prefix}_lines_total{{endpoint="{_escape(endpoint)}"}} {value}\n'
            )

        out.append(f"# HELP {prefix}_state_saves_total 状态快照次数。\n")
        out.append(f"# TYPE {prefix}_state_saves_total counter\n")
        for result, value in sorted(saves.items()):
            out.append(
                f'{prefix}_state_saves_total{{result="{_escape(result)}"}} {value}\n'
            )

        out.append(f"# HELP {prefix}_rejected_total 被拒的请求数。\n")
        out.append(f"# TYPE {prefix}_rejected_total counter\n")
        for reason, value in sorted(rejected.items()):
            out.append(
                f'{prefix}_rejected_total{{reason="{_escape(reason)}"}} {value}\n'
            )

        return "".join(out)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
