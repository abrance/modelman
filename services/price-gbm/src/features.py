"""特征工程：广东现货日前小时电价（与训练共用同一实现）。

特征只用「锚点 A = 目标日 X - horizon 天」及更早的数据，线上预测与训练同口径，
不会把真实值泄进特征。锚点或锚点日数据缺失时返回 None（调用方折算 400）。

22 维：
  lags(5)   同小时价：A / A-1 / A-2 / A-7 / A-14
  roll(4)   同小时 7/30 天均值与标准差（含 A）
  day(5)    锚点日 24 点 min/max/mean、锚点日平均电量、同小时电量
  cal(5)    小时、星期、月、周末、法定节假日
  extra(3)  近 4 个同星期同小时均值、3 天/10 天日均趋势比、锚点日小时形状比
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

NAN = float("nan")

HOLIDAYS: set[str] = set()


def _rng(a: str, b: str) -> None:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    while d0 <= d1:
        HOLIDAYS.add(d0.isoformat())
        d0 += timedelta(days=1)


def _init_holidays() -> None:
    """2024-2026 法定节假日（元旦/春节/清明/五一/端午/中秋/国庆）。

    shortcut: 目标日在表外时节假日特征恒为 0，2027 起需要补表。
    """
    for y in (2024, 2025, 2026):
        HOLIDAYS.add(f"{y}-01-01")
    for a, b in (
        ("2024-02-10", "2024-02-17"), ("2025-01-28", "2025-02-04"), ("2026-02-15", "2026-02-22"),
        ("2024-04-04", "2024-04-06"), ("2025-04-04", "2025-04-06"), ("2026-04-04", "2026-04-06"),
        ("2024-05-01", "2024-05-05"), ("2025-05-01", "2025-05-05"), ("2026-05-01", "2026-05-05"),
        ("2024-06-08", "2024-06-10"), ("2025-05-31", "2025-06-02"), ("2026-06-19", "2026-06-21"),
        ("2024-09-15", "2024-09-17"), ("2025-10-01", "2025-10-08"), ("2026-09-25", "2026-09-27"),
        ("2024-10-01", "2024-10-07"), ("2026-10-01", "2026-10-07"),
    ):
        _rng(a, b)


_init_holidays()


def day_features(
    days: dict[str, dict[str, list[float]]],
    x_day: date,
    hour: int,
    horizon: int,
) -> list[float] | None:
    """见模块 docstring。days 的键为 ISO 日期，值含 prices/power 各 24 点。"""
    anchor = x_day - timedelta(days=horizon)
    anchor_day = days.get(anchor.isoformat())
    if anchor_day is None:
        return None
    prices_a = np.asarray(anchor_day["prices"], dtype=float)

    def price(d: date) -> float:
        row = days.get(d.isoformat())
        return row["prices"][hour] if row else NAN

    def power(d: date) -> float:
        row = days.get(d.isoformat())
        return row["power"][hour] if row else NAN

    lags = [price(anchor - timedelta(days=k)) for k in (0, 1, 2, 7, 14)]

    roll = []
    for win in (7, 30):
        seq = np.asarray([price(anchor - timedelta(days=k)) for k in range(win)], dtype=float)
        seq = seq[seq == seq]
        roll += [float(seq.mean()) if seq.size else NAN, float(seq.std()) if seq.size else NAN]

    power_a = np.asarray(anchor_day.get("power", []), dtype=float)
    daystat = [
        float(prices_a.min()), float(prices_a.max()), float(prices_a.mean()),
        float(power_a.mean()) if power_a.size else NAN, power(anchor),
    ]

    dows: list[float] = []
    cur, steps = anchor, 0
    while len(dows) < 4 and steps < 35:
        if cur.weekday() == x_day.weekday():
            value = price(cur)
            if value == value:
                dows.append(value)
        cur -= timedelta(days=1)
        steps += 1
    dow_mean = float(np.mean(dows)) if dows else NAN

    def daymean(d: date) -> float:
        row = days.get(d.isoformat())
        return float(np.mean(row["prices"])) if row else NAN

    short = float(np.nanmean([daymean(anchor - timedelta(days=k)) for k in range(3)]))
    long_ = float(np.nanmean([daymean(anchor - timedelta(days=k)) for k in range(10)]))
    trend = short / long_ if long_ == long_ and long_ != 0 else NAN

    mean_a = prices_a.mean()
    shape = float(prices_a[hour] / mean_a) if mean_a == mean_a and mean_a else NAN

    dow = x_day.weekday()
    cal = [float(hour), float(dow), float(x_day.month),
           1.0 if dow >= 5 else 0.0, 1.0 if x_day.isoformat() in HOLIDAYS else 0.0]
    return lags + roll + daystat + cal + [dow_mean, trend, shape]
