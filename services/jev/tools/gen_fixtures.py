#!/usr/bin/env python3
"""生成契约测试基线。

顺序有依赖：先确认权重摘要与 registry 一致，再逐样本跑判定，最后写入基线。
基线内容包含三部分——每个样本的期望答案、参数指纹（档位与 token 预算）、
以及汇总层的延迟预算；契约测试会断言这三样都没漂。

用法：make fixtures SERVICE=jev
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_ROOT.parent.parent
sys.path.insert(0, str(SERVICE_ROOT))

from src.config import Config  # noqa: E402
from src.engine import DecisionEngine  # noqa: E402
from src.metrics import Metrics  # noqa: E402

FIXTURES = SERVICE_ROOT / "tests" / "fixtures"
DIGESTS = REPO_ROOT / "registry" / "jev-digests.json"

# 概率容差。跨机器的 BLAS 与线程数会让浮点末位变化，逐位相等不是可维护的断言；
# 选项的 argmax 与分数档位仍然要求完全一致。
TOLERANCE = 0.005

# 延迟预算给得宽：只用来捕捉数量级退化（换了线程数、权重没常驻、被限流），
# 不是性能指标。实测 4 线程单问约 100 ms。
LATENCY_BUDGET_MS = 400

# 每个样本重复的次数，用来取中位数
REPEATS = 3


def main() -> int:
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    digests = json.loads(DIGESTS.read_text(encoding="utf-8"))

    config = Config.from_env(
        {
            "MODELS_DIR": str(SERVICE_ROOT / "models"),
            "MODEL_TIER": "multilingual",
            "PRELOAD": "true",
        }
    )
    metrics = Metrics(version="fixtures", commit="fixtures")
    engine = DecisionEngine(config, metrics)
    if not engine.is_ready():
        print(f"权重未就绪，无法生成基线：{engine.load_error}", file=sys.stderr)
        return 2

    results = []
    latencies = []
    for case in cases:
        answers = None
        times = []
        for _ in range(REPEATS):
            outcome = engine.decide(case["state"], case["questions"])
            answers = outcome.answers
            times.append(outcome.elapsed_ms)
        latencies.append(statistics.median(times))
        results.append(
            {
                "id": case["id"],
                "note": case["note"],
                "answers": answers,
                "median_ms": round(statistics.median(times), 1),
            }
        )
        print(f"{case['id']}: {json.dumps(answers, ensure_ascii=False)[:160]}")

    baseline = {
        "_comment": (
            "由 tools/gen_fixtures.py 生成，提交前必须人工确认结果合理。"
            "answers 逐键比对（概率按 tolerance），延迟只查数量级退化。"
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


if __name__ == "__main__":
    started = time.monotonic()
    code = main()
    print(f"elapsed {time.monotonic() - started:.1f}s")
    raise SystemExit(code)
