"""契约测试：固定样本比对基线。

门禁两件事：
1. 逐样本数值在容差内一致（模型与特征改了就要人工 review 基线差异）；
2. 汇总延迟不劣化一个数量级（阈值放得宽，只抓量级退化）。
"""

from __future__ import annotations

import json

import pytest

from src.engine import PriceEngine
from src.models import DayItem

from .conftest import FIXTURES, build_config, requires_models

TOLERANCE = 0.001


def _baseline() -> dict:
    return json.loads((FIXTURES / "baseline.json").read_text(encoding="utf-8"))


@requires_models
def test_outputs_match_baseline() -> None:
    baseline = _baseline()
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    engine = PriceEngine(build_config())
    assert engine.model_metadata()["revision"] == baseline["revision"]

    assert len(cases) == len(baseline["outputs"])
    for case, expected in zip(cases, baseline["outputs"]):
        outcome = engine.forecast(
            [DayItem(date=d["date"], values=d["values"], power=d["power"])
             for d in case["days"]],
            case["target"],
        )
        assert outcome.horizon_days == expected["horizon_days"]
        for hour, (got, want) in enumerate(zip(outcome.point, expected["point"])):
            assert abs(got - want) <= TOLERANCE, f"{case['target']} {hour}:00 点预测漂移 {got} vs {want}"
        for level in ("0.1", "0.5", "0.9"):
            for hour, (got, want) in enumerate(zip(outcome.quantiles[level], expected["quantiles"][level])):
                assert abs(got - want) <= TOLERANCE, f"{level} 分位 {hour}:00 漂移"


@requires_models
def test_latency_budget() -> None:
    baseline = _baseline()
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    engine = PriceEngine(build_config())
    total_ms = 0.0
    for case in cases:
        import time

        started = time.perf_counter()
        engine.forecast(
            [DayItem(date=d["date"], values=d["values"], power=d["power"])
             for d in case["days"]],
            case["target"],
        )
        total_ms += (time.perf_counter() - started) * 1000.0
    # 预算按一个数量级给：基线毫秒级，退化到秒级才算失败
    assert total_ms < baseline["latency_budget_ms"] * 10


@requires_models
def test_quantiles_monotonic() -> None:
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    engine = PriceEngine(build_config())
    for case in cases:
        outcome = engine.forecast(
            [DayItem(date=d["date"], values=d["values"], power=d["power"])
             for d in case["days"]],
            case["target"],
        )
        q10, q50, q90 = (outcome.quantiles[k] for k in ("0.1", "0.5", "0.9"))
        for hour in range(24):
            assert q10[hour] <= q50[hour] <= q90[hour]
