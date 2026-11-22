#!/usr/bin/env python3
"""训练广东现货日前小时电价的 LightGBM 分位模型（q10/q50/q90）。

输入：training-data/gd-home24h.json —— 官网主页 24 点分时的完整日快照
（{date: {prices: [24], power: [24]}}，匿名网关采集，公开市场数据）。

输出：models/gbm-hour-v1/{q10,q50,q90}.txt 与 sha256.json（digest 清单，
与 forecast 服务同款约定：运行期启动时逐文件核对）。

确定性：seed 钉死、无 bagging、单线程训练，同一份输入必出同一份权重
（契约测试的基线依赖这一点）。

口径：特征只用「目标日 X 往前 H 天」及更早的数据（锚点 A = X - H），
与线上预测的生产口径一致，不会把真实值泄进特征。

用法：
  python tools/train.py [--horizon-days 5]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import lightgbm as lgb

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.features import day_features  # noqa: E402 - 训练与推理共用同一实现

DATA_PATH = SERVICE_ROOT / "training-data" / "gd-home24h.json"
MODEL_DIR = SERVICE_ROOT / "models" / "gbm-hour-v1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="1,2,3,4,5,6,7,8,10,12,14",
                    help="训练用的锚点距离集合（逗号分隔）。必须覆盖线上会遇到的整个区间："
                         "生产口径约 5~6 天，准确度统计 as-of 重放会遇到 1~14 天。"
                         "只用一个 horizon 训出来的模型在别的 horizon 上会明显变差。")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    days = {k: {"prices": v["prices"], "power": v["power"]} for k, v in raw.items()}
    all_days = sorted(date.fromisoformat(d) for d in days)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    print(f"{len(all_days)} 个完整日（{all_days[0]} ~ {all_days[-1]}），horizons={horizons}")

    X, y = [], []
    for horizon in horizons:
        for x_day in all_days:
            target = days[x_day.isoformat()]["prices"]
            for h in range(24):
                f = day_features(days, x_day, h, horizon)
                if f is None:
                    break
                X.append(f)
                y.append(target[h])
    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    print(f"训练样本 {X.shape}")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for level, name in ((0.1, "q10.txt"), (0.5, "q50.txt"), (0.9, "q90.txt")):
        booster = lgb.train(
            {
                "objective": "quantile", "alpha": level,
                "num_leaves": 63, "learning_rate": 0.05, "min_data_in_leaf": 50,
                "num_threads": 1, "seed": args.seed,
                "verbosity": -1,
            },
            lgb.Dataset(X, y),
            num_boost_round=500,
        )
        path = MODEL_DIR / name
        booster.save_model(str(path), num_iteration=booster.best_iteration)
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"saved {path}")

    manifest["revision"] = (
        f"gd-home24h-{all_days[0]}~{all_days[-1]}-H{args.horizons.replace(',', '-')}-seed{args.seed}"
    )
    (MODEL_DIR / "sha256.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("revision:", manifest["revision"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
