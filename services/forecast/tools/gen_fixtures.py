#!/usr/bin/env python3
"""生成契约测试基线。

顺序有依赖：先确认权重摘要与 registry 一致，再逐样本跑预测，最后写入基线。
基线内容包含四部分——每个样本的点预测与分位预测、参数指纹、数值容差、
以及汇总层的延迟预算；契约测试会断言这四样都没漂。

用法：make fixtures SERVICE=forecast
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

SERVICE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_ROOT.parent.parent
sys.path.insert(0, str(SERVICE_ROOT))

from src.config import Config  # noqa: E402
from src.engine import ForecastEngine, prepare_series, resolve_levels, validate_request  # noqa: E402
from src.metrics import Metrics  # noqa: E402
from src.models import ForecastRequest, Series  # noqa: E402

FIXTURES = SERVICE_ROOT / "tests" / "fixtures"
DIGESTS = REPO_ROOT / "registry" / "forecast-digests.json"

# 数值容差。跨机器的 BLAS 与线程数会让浮点末位变化，逐位相等不是可维护的断言。
# 数值量级在 1e1..1e2，1e-3 绝对容差比"相对 1e-5"更好解释。
TOLERANCE = 1e-3

# 延迟预算给得宽：只用来捕捉数量级退化（换了线程数、权重没常驻、被限流），
# 不是性能指标。实测 4 线程、512 点上下文、48 步长约 1.3 s。
LATENCY_BUDGET_MS = 8000

# 每个样本重复的次数，用来取中位数
REPEATS = 3


def main() -> int:
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    digests = json.loads(DIGESTS.read_text(encoding="utf-8"))

    config = Config.from_env(
        {
            "MODELS_DIR": str(SERVICE_ROOT / "models"),
            "MODEL_TIER": "timesfm-3.0",
            "PRELOAD": "true",
        }
    )
    metrics = Metrics(version="fixtures", commit="fixtures")
    engine = ForecastEngine(config, metrics)
    if not engine.is_ready():
        print(f"权重未就绪，无法生成基线：{engine.load_error}", file=sys.stderr)
        return 2

    results = []
    latencies = []
    for case in cases:
        request = ForecastRequest(
            series=[Series(**item) for item in case["series"]],
            horizon=case["horizon"],
            quantiles=case.get("quantiles"),
        )
        validate_request(request, config)
        series, _truncated = prepare_series(request.series, config)
        levels = resolve_levels(request, engine.levels)

        outcome = None
        times = []
        for _ in range(REPEATS):
            outcome = engine.forecast(series, request.horizon, levels)
            times.append(outcome.elapsed_ms)
        latencies.append(statistics.median(times))

        results.append(
            {
                "id": case["id"],
                "note": case["note"],
                "horizon": request.horizon,
                "levels": outcome.levels,
                "forecasts": [
                    {
                        "id": item.id,
                        "point": _round(item.point),
                        "quantiles": {
                            level: _round(values)
                            for level, values in item.quantiles.items()
                        },
                    }
                    for item in outcome.items
                ],
                "median_ms": round(statistics.median(times), 1),
            }
        )
        point = results[-1]["forecasts"][0]["point"]
        print(f"{case['id']}: horizon={request.horizon} levels={outcome.levels} first3={point[:3]}")

    baseline = {
        "_comment": (
            "由 tools/gen_fixtures.py 生成，提交前必须人工确认结果合理。"
            "点预测与分位预测按 tolerance 比对，并断言分位单调性与输出形状；"
            "延迟只查数量级退化。"
        ),
        "model": {
            "tier": config.model_tier,
            "device": config.device,
            "revision": digests["revision"],
            "files": digests["files"],
        },
        "params": config.profile(),
        "tolerance": TOLERANCE,
        "latency_budget_ms": LATENCY_BUDGET_MS,
        "median_latency_ms": round(statistics.median(latencies), 1),
        "cases": results,
    }
    (FIXTURES / "baseline.json").write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"baseline written: {len(results)} cases, "
        f"median {baseline['median_latency_ms']} ms, budget {LATENCY_BUDGET_MS} ms"
    )
    return 0


def _round(array: np.ndarray):
    """基线里存小数点后 4 位：够容差用，也不会让文件膨胀。"""
    value = np.round(np.asarray(array, dtype=np.float64), 4)
    if value.ndim == 1:
        return [float(item) for item in value]
    return [[float(item) for item in row] for row in value]


if __name__ == "__main__":
    started = time.monotonic()
    code = main()
    print(f"elapsed {time.monotonic() - started:.1f}s")
    raise SystemExit(code)
