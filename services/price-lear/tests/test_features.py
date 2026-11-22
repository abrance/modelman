"""特征层测试：口径（只用 ≤ 锚点数据）、缺失日容错、维度。"""

from __future__ import annotations

import json
from datetime import date

from src.features import HOLIDAYS, day_features

SERVICE_ROOT = __file__.rsplit("/tests/", 1)[0]
DATA = json.loads(
    open(f"{SERVICE_ROOT}/training-data/gd-home24h.json", encoding="utf-8").read()
)


def days_map() -> dict:
    return {k: {"prices": v["prices"], "power": v["power"]} for k, v in DATA.items()}


def test_feature_dimensions() -> None:
    row = day_features(days_map(), date(2026, 11, 20), 12, 5)
    assert row is not None and len(row) == 22


def test_anchor_missing_returns_none() -> None:
    # 2026-08-09 官网只发了 6 小时，快照里整日弃用 → 锚点缺失必须返回 None
    assert day_features(days_map(), date(2026, 8, 14), 3, 5) is None


def test_no_leakage_features_stable_when_target_far() -> None:
    """锚点前的数据没变时，特征必须逐位一致——这是「不泄真实值」的必要条件。

    把目标日后的真实数据全部删掉，特征不变。
    """
    days = days_map()
    base = day_features(days, date(2026, 11, 12), 7, 5)
    trimmed = {k: v for k, v in days.items() if k <= "2026-11-07"}
    assert day_features(trimmed, date(2026, 11, 12), 7, 5) == base


def test_holiday_table_covers_contract_window() -> None:
    assert "2026-10-01" in HOLIDAYS
    assert "2025-01-01" in HOLIDAYS
    # 非节假日
    assert "2026-11-20" not in HOLIDAYS
