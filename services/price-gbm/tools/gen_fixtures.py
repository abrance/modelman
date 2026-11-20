#!/usr/bin/env python3
"""契约基线生成：从训练数据快照构造请求样本，跑引擎记录期望输出。

基线只能用本程序更新，且必须人工 review 差异——改基线等于改验收标准。
样本刻意选自训练数据末尾：这是采集窗口最常见、特征最完整的形态。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.config import Config  # noqa: E402
from src.engine import PriceEngine  # noqa: E402
from src.models import DayItem  # noqa: E402

FIXTURES = SERVICE_ROOT / "tests" / "fixtures"
DATA = json.loads((SERVICE_ROOT / "training-data" / "gd-home24h.json").read_text(encoding="utf-8"))


def day_payload(key: str) -> dict:
    row = DATA[key]
    return {"date": key, "values": row["prices"], "power": row["power"]}


def main() -> int:
    keys = sorted(DATA)[-40:]
    target = (date.fromisoformat(keys[-1]) + timedelta(days=5)).isoformat()
    cases = [{"days": [day_payload(k) for k in keys], "target": target}]

    config = Config.from_env({"MODELS_DIR": str(SERVICE_ROOT / "models"), "PRELOAD": "true"})
    engine = PriceEngine(config)
    if not engine.is_ready():
        print("engine not ready:", engine.load_error, file=sys.stderr)
        return 1

    outputs = []
    latencies = []
    for case in cases:
        started = time.perf_counter()
        outcome = engine.forecast(
            [DayItem(**d) for d in case["days"]], case["target"]
        )
        latencies.append((time.perf_counter() - started) * 1000.0)
        outputs.append({
            "target": outcome.target,
            "horizon_days": outcome.horizon_days,
            "point": [round(float(v), 4) for v in outcome.point],
            "quantiles": {
                level: [round(float(v), 4) for v in values]
                for level, values in outcome.quantiles.items()
            },
        })

    baseline = {
        "revision": engine.model_metadata()["revision"],
        "tolerance": 0.001,
        "latency_budget_ms": 2000,
        "sum_latency_ms": round(sum(latencies), 2),
        "outputs": outputs,
    }
    (FIXTURES / "cases.json").write_text(
        json.dumps({"cases": cases}, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (FIXTURES / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"fixtures written: {len(cases)} case, sum_latency={baseline['sum_latency_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
