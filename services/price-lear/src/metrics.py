"""Prometheus 指标：请求计数与延迟直方图（按端点打标签）+ 模型状态。

比 forecast 的实现小得多：没有分位截断计数、没有槽位拒绝计数——
LEAR 线性推理毫秒级，不存在排队与截断这两件事。
"""

from __future__ import annotations

import threading
import time

METRIC_PREFIX = "modelman_price_lear"


class Metrics:
    def __init__(self, version: str, commit: str) -> None:
        self._lock = threading.Lock()
        self._uptime_started = time.monotonic()
        self._counters: dict[tuple[str, str], int] = {}
        self._latencies: dict[str, list[float]] = {}
        self._model_loaded = False
        self._model_load_seconds = 0.0
        self._model_error: str | None = None
        self._version = version
        self._commit = commit

    def set_model(self, loaded: bool, load_seconds: float, error: str | None) -> None:
        with self._lock:
            self._model_loaded = loaded
            self._model_load_seconds = load_seconds
            self._model_error = error

    def record_success(self, endpoint: str, seconds: float) -> None:
        with self._lock:
            self._counters[(endpoint, "success")] = self._counters.get((endpoint, "success"), 0) + 1
            self._latencies.setdefault(endpoint, []).append(seconds)

    def record_failure(self, endpoint: str, reason: str) -> None:
        with self._lock:
            self._counters[(endpoint, reason)] = self._counters.get((endpoint, reason), 0) + 1

    def render(self, uptime_secs: float) -> str:
        with self._lock:
            lines = [
                f"{METRIC_PREFIX}_info{{version=\"{self._version}\",commit=\"{self._commit}\"}} 1",
                f"{METRIC_PREFIX}_uptime_seconds {uptime_secs:.3f}",
                f"{METRIC_PREFIX}_model_loaded {1 if self._model_loaded else 0}",
                f"{METRIC_PREFIX}_model_load_seconds {self._model_load_seconds:.3f}",
            ]
            for (endpoint, outcome), value in sorted(self._counters.items()):
                lines.append(
                    f"{METRIC_PREFIX}_requests_total{{endpoint=\"{endpoint}\",outcome=\"{outcome}\"}} {value}"
                )
            for endpoint, samples in sorted(self._latencies.items()):
                if not samples:
                    continue
                ordered = sorted(samples)
                p50 = ordered[int(0.50 * (len(ordered) - 1))]
                p95 = ordered[int(0.95 * (len(ordered) - 1))]
                lines.append(
                    f"{METRIC_PREFIX}_latency_seconds{{endpoint=\"{endpoint}\",quantile=\"0.5\"}} {p50:.6f}"
                )
                lines.append(
                    f"{METRIC_PREFIX}_latency_seconds{{endpoint=\"{endpoint}\",quantile=\"0.95\"}} {p95:.6f}"
                )
            if self._model_error:
                escaped = self._model_error.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f"{METRIC_PREFIX}_model_load_error{{message=\"{escaped}\"}} 1")
            return "\n".join(lines) + "\n"
