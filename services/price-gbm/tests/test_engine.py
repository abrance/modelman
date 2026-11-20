"""引擎层测试：请求校验（天数/形状/日期/超前）、分位单调、就绪口径。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.engine import InvalidRequest, PriceEngine
from src.models import DayItem

from .conftest import build_config, requires_models


def make_days(n: int = 40, start: str = "2026-10-07") -> list[DayItem]:
    base = date.fromisoformat(start)
    return [
        DayItem(date=(base + timedelta(days=k)).isoformat(),
                values=[float(300 + h) for h in range(24)],
                power=[float(1000 + h) for h in range(24)])
        for k in range(n)
    ]


@requires_models
def test_forecast_shape_and_monotonic() -> None:
    engine = PriceEngine(build_config())
    assert engine.is_ready()
    outcome = engine.forecast(make_days(), "2026-11-20")
    assert len(outcome.point) == 24
    assert set(outcome.quantiles) == {"0.1", "0.5", "0.9"}
    q10, q50, q90 = (outcome.quantiles[k] for k in ("0.1", "0.5", "0.9"))
    for hour in range(24):
        assert q10[hour] <= q50[hour] <= q90[hour]
    # 点预测 = 0.5 分位
    assert outcome.point.tolist() == q50.tolist()


@requires_models
def test_horizon_days_computed_from_anchor() -> None:
    engine = PriceEngine(build_config())
    assert engine.forecast(make_days(), "2026-11-20").horizon_days == 5


def test_validation_errors() -> None:
    engine = PriceEngine(build_config())
    with pytest.raises(InvalidRequest):
        engine.forecast([], "2026-11-20")  # 空 days
    with pytest.raises(InvalidRequest):
        engine.forecast(make_days(61), "2026-11-20")  # 超过 MAX_DAYS
    with pytest.raises(InvalidRequest):
        # values 不是 24 点
        engine.forecast([DayItem(date="2026-11-15", values=[1.0, 2.0])], "2026-11-20")
    with pytest.raises(InvalidRequest):
        # 日期非法
        engine.forecast([DayItem(date="2026/11/15", values=[1.0] * 24)], "2026-11-20")
    with pytest.raises(InvalidRequest):
        # 目标日不晚于锚点（锚点是最后一天 2026-11-15）
        engine.forecast(make_days(), "2026-11-15")
    with pytest.raises(InvalidRequest):
        # 超过 MAX_HORIZON_DAYS（锚点 2026-11-15 + 14 天是上限）
        engine.forecast(make_days(), "2026-12-01")


def test_unsorted_days_rejected() -> None:
    engine = PriceEngine(build_config())
    days = make_days(3)
    days[0], days[1] = days[1], days[0]
    with pytest.raises(InvalidRequest):
        engine.forecast(days, "2026-11-20")


def test_corrupt_weights_not_ready(monkeypatch) -> None:
    """摘要不符时不就绪，但进程口径（is_ready False + 原因可读）保持。"""
    config = build_config(MODELS_DIR="does-not-exist", VERIFY_WEIGHTS="false")
    engine = PriceEngine(config)
    assert not engine.is_ready()
    assert engine.load_error
