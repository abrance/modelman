"""Prometheus 文本暴露，零外部依赖。

口径与 OCR、日志聚类一致：请求计数与延迟直方图带端点标签。这里的服务特有的
量是判定规模（问题数、token 数）、输入被截断的次数，以及常驻内存——内存是
这个服务的主要成本，把它报出来，容量问题才不用去容器里猜。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

# 一次请求要跑完它携带的所有问题，CPU 上单问在百毫秒量级，
# 所以桶从 5 ms 起、最慢给到 30 s，用来发现"某次请求把槽位堵住"。
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

METRIC_PREFIX = "jev"


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
        self._questions: dict[str, int] = defaultdict(int)
        self._tokens: dict[str, int] = defaultdict(int)
        self._rejected: dict[str, int] = defaultdict(int)
        self._truncated = 0
        self._ready = 0
        self._load_error = 0
        self._load_seconds = 0.0

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
        self, endpoint: str, seconds: float, questions: int, tokens: dict | None = None
    ) -> None:
        with self._lock:
            self._requests[(endpoint, "ok")] += 1
            self._durations[endpoint].observe(seconds)
            self._questions[endpoint] += questions
            for kind in ("input_tokens", "output_tokens"):
                value = (tokens or {}).get(kind)
                if isinstance(value, int) and value > 0:
                    self._tokens[kind] += value

    def record_failure(self, endpoint: str, status: str) -> None:
        with self._lock:
            self._requests[(endpoint, status)] += 1

    def record_rejected(self, reason: str) -> None:
        with self._lock:
            self._rejected[reason] += 1

    def record_truncated(self, count: int = 1) -> None:
        with self._lock:
            self._truncated += count

    def set_model(self, ready: bool, load_seconds: float, load_error: str | None) -> None:
        with self._lock:
            self._ready = 1 if ready else 0
            self._load_error = 1 if load_error else 0
            self._load_seconds = load_seconds

    # ── 暴露 ────────────────────────────────────────────────────────────

    def render(self, uptime_secs: float) -> str:
        with self._lock:
            in_flight = self._in_flight
            requests = dict(self._requests)
            durations = {
                key: (list(value.buckets), value.sum, value.count)
                for key, value in self._durations.items()
            }
            questions = dict(self._questions)
            tokens = dict(self._tokens)
            rejected = dict(self._rejected)
            truncated = self._truncated
            ready, load_error, load_seconds = (
                self._ready,
                self._load_error,
                self._load_seconds,
            )

        prefix = METRIC_PREFIX
        out: list[str] = []

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

        out.append(f"# HELP {prefix}_model_ready 默认档位已常驻且可接流量时为 1。\n")
        out.append(f"# TYPE {prefix}_model_ready gauge\n")
        out.append(f"{prefix}_model_ready {ready}\n")

        out.append(f"# HELP {prefix}_model_load_error 权重加载失败时为 1。\n")
        out.append(f"# TYPE {prefix}_model_load_error gauge\n")
        out.append(f"{prefix}_model_load_error {load_error}\n")

        out.append(f"# HELP {prefix}_model_load_seconds 权重加载耗时。\n")
        out.append(f"# TYPE {prefix}_model_load_seconds gauge\n")
        out.append(f"{prefix}_model_load_seconds {load_seconds:.3f}\n")

        out.append(f"# HELP {prefix}_requests_in_flight 正在执行的请求数。\n")
        out.append(f"# TYPE {prefix}_requests_in_flight gauge\n")
        out.append(f"{prefix}_requests_in_flight {in_flight}\n")

        out.append(
            f"# HELP {prefix}_process_resident_memory_bytes 进程常驻内存。\n"
        )
        out.append(f"# TYPE {prefix}_process_resident_memory_bytes gauge\n")
        out.append(f"{prefix}_process_resident_memory_bytes {resident_bytes()}\n")

        out.append(f"# HELP {prefix}_truncated_inputs_total 输入超出 token 预算被截断的次数。\n")
        out.append(f"# TYPE {prefix}_truncated_inputs_total counter\n")
        out.append(f"{prefix}_truncated_inputs_total {truncated}\n")

        out.append(f"# HELP {prefix}_requests_total 按端点与结果计数的请求数。\n")
        out.append(f"# TYPE {prefix}_requests_total counter\n")
        for (endpoint, status), count in sorted(requests.items()):
            out.append(
                f'{prefix}_requests_total{{endpoint="{_escape(endpoint)}",'
                f'status="{_escape(status)}"}} {count}\n'
            )

        out.append(f"# HELP {prefix}_questions_total 按端点计数的问题数。\n")
        out.append(f"# TYPE {prefix}_questions_total counter\n")
        for endpoint, count in sorted(questions.items()):
            out.append(
                f'{prefix}_questions_total{{endpoint="{_escape(endpoint)}"}} {count}\n'
            )

        out.append(f"# HELP {prefix}_tokens_total 上游上报的 token 用量。\n")
        out.append(f"# TYPE {prefix}_tokens_total counter\n")
        for kind, count in sorted(tokens.items()):
            out.append(f'{prefix}_tokens_total{{kind="{_escape(kind)}"}} {count}\n')

        out.append(f"# HELP {prefix}_rejected_total 按原因计数的被拒请求。\n")
        out.append(f"# TYPE {prefix}_rejected_total counter\n")
        for reason, count in sorted(rejected.items()):
            out.append(
                f'{prefix}_rejected_total{{reason="{_escape(reason)}"}} {count}\n'
            )

        out.append(
            f"# HELP {prefix}_request_duration_seconds 请求耗时直方图。\n"
        )
        out.append(f"# TYPE {prefix}_request_duration_seconds histogram\n")
        for endpoint, (buckets, total, count) in sorted(durations.items()):
            label = _escape(endpoint)
            for index, bound in enumerate(DURATION_BUCKETS):
                out.append(
                    f'{prefix}_request_duration_seconds_bucket{{endpoint="{label}",'
                    f'le="{bound}"}} {buckets[index]}\n'
                )
            out.append(
                f'{prefix}_request_duration_seconds_bucket{{endpoint="{label}",'
                f'le="+Inf"}} {count}\n'
            )
            out.append(
                f'{prefix}_request_duration_seconds_sum{{endpoint="{label}"}} {total:.6f}\n'
            )
            out.append(
                f'{prefix}_request_duration_seconds_count{{endpoint="{label}"}} {count}\n'
            )

        return "".join(out)


def resident_bytes() -> int:
    """常驻内存（VmRSS）。读不到就报 0，不要让指标端点本身出错。"""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
