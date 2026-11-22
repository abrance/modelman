#!/usr/bin/env python3
"""训练广东现货日前小时电价的 LEAR 模型（Lago et al. 2021）。

输入：training-data/gd-home24h.json —— 官网主页 24 点分时的完整日快照
（{date: {prices: [24], power: [24]}}，匿名网关采集，公开市场数据）。

流程：features（与线上同源）→ SimpleImputer(median) → StandardScaler → LassoCV
（TimeSeriesSplit 选 alpha）。导出成一组**纯数值** predictor 状态（填充中位数、
标准化 mean/scale、LASSO 系数/截距、训练残差的 q10/q90 经验分位偏移），
运行期推理只用 numpy，不依赖 sklearn。

分位带说明：LEAR 本体只给点预测，q10/q90 是训练残差的经验分位数作固定偏移，
不是模型原生分位（与 ems benchmark 同口径，已在 README 标注）。

确定性：LassoCV 随机种子、拟合全程单线程，同一份输入必出同一份系数（契约基线依赖）。

口径：特征只用「目标日 X 往前 H 天」及更早的数据（锚点 A = X - H），与线上一致。

用法：
  python tools/train.py [--horizon-days 5] [--seed 42]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np

# 钉死线程数：LassoCV 的多路拟合在不同核数机器上可能有浮点微差，单线程保证确定性。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LassoCV  # noqa: E402
from sklearn.model_selection import TimeSeriesSplit  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.features import day_features  # noqa: E402 - 训练与推理共用同一实现

DATA_PATH = SERVICE_ROOT / "training-data" / "gd-home24h.json"
MODEL_DIR = SERVICE_ROOT / "models" / "lear-hour-v1"

LEVELS = (0.1, 0.9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-days", type=int, default=5,
                    help="训练口径的锚点距离（生产口径：官网 T+5 滞后后目标日距锚点约 5 天）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    days = {k: {"prices": v["prices"], "power": v["power"]} for k, v in raw.items()}
    all_days = sorted(date.fromisoformat(d) for d in days)
    print(f"{len(all_days)} 个完整日（{all_days[0]} ~ {all_days[-1]}），H={args.horizon_days}")

    X, y = [], []
    for x_day in all_days:
        target = days[x_day.isoformat()]["prices"]
        for h in range(24):
            f = day_features(days, x_day, h, args.horizon_days)
            if f is None:
                break
            X.append(f)
            y.append(target[h])
    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    print(f"训练样本 {X.shape}")

    imputer = SimpleImputer(strategy="median")
    X_imp = imputer.fit_transform(X)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_imp)
    lasso = LassoCV(cv=TimeSeriesSplit(n_splits=3), random_state=args.seed, max_iter=5000)
    lasso.fit(X_scaled, y)
    resid = y - lasso.predict(X_scaled)
    off_lo, off_hi = (float(v) for v in np.quantile(resid, LEVELS))
    print(f"LassoCV alpha={lasso.alpha_:.6f}，非zero系数={np.count_nonzero(lasso.coef_)}/{len(lasso.coef_)}")

    state = {
        "impute_medians": imputer.statistics_.tolist(),
        "scaler_mean": scaler.mean_.tolist(),
        "scaler_scale": scaler.scale_.tolist(),
        "coef": lasso.coef_.tolist(),
        "intercept": float(lasso.intercept_),
        "offset_q10": off_lo,
        "offset_q90": off_hi,
        "feature_names": [
            "lag_h", "lag_h-1", "lag_h-2", "lag_h-7", "lag_h-14",
            "roll7_mean", "roll7_std", "roll30_mean", "roll30_std",
            "day_min", "day_max", "day_mean", "day_power_mean", "hour_power",
            "hour", "dow", "month", "weekend", "holiday",
            "dow_mean", "trend", "shape",
        ],
    }

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    state_path = MODEL_DIR / "state.json"
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "state.json": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        "revision": (
            f"gd-home24h-{all_days[0]}~{all_days[-1]}-H{args.horizon_days}-seed{args.seed}"
        ),
    }
    (MODEL_DIR / "sha256.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"saved {state_path}")
    print("revision:", manifest["revision"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
