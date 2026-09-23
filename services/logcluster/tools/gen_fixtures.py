#!/usr/bin/env python3
"""生成契约测试基线。

对应 Rust 服务的 `src/bin/gen-fixtures.rs`。用法：

    make fixtures SERVICE=logcluster

流程与 OCR 一致：所有样本按**固定顺序**喂给同一棵从空开始的状态树
（生产里也是累积学习，不是每个样本各起一棵树），把每行的模板、
每个样本结束时的模板数、最终模板清单与延迟预算写进基线。

基线变更等于改验收标准，提交前必须人工看一遍生成的模板是否合理。

为什么顺序固定：drain3 先到先得地建簇，`cluster_id` 与合并结果都依赖
输入顺序。基线里连 `cluster_id` 一起记下来，正是为了让顺序变化也被发现。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))

from src.config import Config
from src.engine import DrainEngine
from src.metrics import Metrics

# 基线用的参数就是服务默认参数，测试里会再断言一次这两者一致
BASELINE_PROFILE_ENV = {
    "SIM_TH": "0.4",
    "DEPTH": "4",
    "MAX_CHILDREN": "100",
    "MASK_RULES": "",
    "PARAMETRIZE_NUMERIC": "true",
    "SNAPSHOT_COMPRESS": "true",
}

# 延迟预算的算法：取实测的 10 倍，下限 2 秒。故意放得很宽——
# 它用来抓"数量级退化"，不是抓抖动。
LATENCY_FACTOR = 10
LATENCY_FLOOR_MS = 2000.0

# 只读匹配的留出样本：不在上面任何样本里出现，用来验证学到的模板能被复用
MATCH_PROBES = [
    "2026-09-23T10:09:01Z INFO user frank logged in from 10.0.0.9",
    "disk usage 77% on /dev/sdc3",
    "2026-09-23T10:09:02Z DEBUG cache hit ratio 0.93",
]


def read_case(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def case_paths(fixtures: Path) -> list[Path]:
    return sorted(fixtures.glob("case_*.txt"))


def main() -> None:
    fixtures = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else SERVICE_ROOT / "tests" / "fixtures"
    )
    if not fixtures.is_dir():
        raise SystemExit(f"样本目录不存在：{fixtures}")

    paths = case_paths(fixtures)
    if not paths:
        raise SystemExit(f"{fixtures} 下没有 case_*.txt")

    with tempfile.TemporaryDirectory(prefix="logcluster-fixtures-") as tmp:
        # 生成基线时不要读写仓库里的 .state：基线必须从空树开始
        env = dict(BASELINE_PROFILE_ENV)
        state_dir = Path(tmp) / "state"
        env["STATE_DIR"] = str(state_dir)
        config = Config.from_env(env)
        engine = DrainEngine(config, Metrics(version="fixtures", commit="fixtures"))

        cases = []
        started = time.perf_counter()
        for path in paths:
            name = path.stem
            lines = read_case(path)
            outcome = engine.cluster(lines)
            clusters, total = engine.counts()
            expected = {
                "name": name,
                "line_count": len(lines),
                "templates": [item["template"] for item in outcome.results],
                "cluster_ids": [item["cluster_id"] for item in outcome.results],
                "change_types": [item["change_type"] for item in outcome.results],
                "cluster_count_after": clusters,
                "total_size_after": total,
            }
            (fixtures / f"{name}.expected.json").write_text(
                json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            cases.append(expected)
            print(f"{name}: {len(lines)} 行 -> {clusters} 个模板")

        total_ms = (time.perf_counter() - started) * 1000.0

        # 快照必须在重放之前取：重放会再次命中模板，把 size 记大
        listing, total_size = engine.list_clusters()

        match_outcome = engine.match(MATCH_PROBES)

        # 不变量：重启后把同样的输入再喂一遍，不应该产生新模板。
        # 用新引擎跑，顺带验证状态真的能载回来。
        restarted = DrainEngine(config, Metrics(version="fixtures", commit="fixtures"))
        replayed = 0
        for case in cases:
            for line in read_case(fixtures / f"{case['name']}.txt"):
                if (
                    restarted.cluster([line]).results[0]["change_type"]
                    == "cluster_created"
                ):
                    replayed += 1

        baseline = {
            "generated_by": "tools/gen_fixtures.py",
            "drain3_version": metadata.version("drain3"),
            "profile": config.profile(),
            "cases": [case["name"] for case in cases],
            "final_cluster_count": len(listing),
            "final_total_size": total_size,
            "templates": listing,
            "replay_new_clusters": replayed,
            "match_probes": [
                {
                    "line": item["line"],
                    "matched": item["matched"],
                    "cluster_id": item["cluster_id"],
                    "template": item["template"],
                }
                for item in match_outcome.results
            ],
            "budget": {
                "measured_total_ms": round(total_ms, 3),
                "total_ms": round(max(LATENCY_FLOOR_MS, total_ms * LATENCY_FACTOR), 3),
            },
        }
        (fixtures / "baseline.json").write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print()
    print(
        f"最终模板数：{baseline['final_cluster_count']}，累计行数：{baseline['final_total_size']}"
    )
    print(f"重放新增模板：{replayed}（应为 0）")
    print(
        f"实测总耗时：{total_ms:.1f} ms，预算：{baseline['budget']['total_ms']:.0f} ms"
    )
    print()
    print("最终模板清单（人工确认是否合理）：")
    for item in listing:
        print(f"  [{item['cluster_id']:>3}] ({item['size']}) {item['template']}")
    print()
    print("只读匹配探针：")
    for item in baseline["match_probes"]:
        mark = item["template"] if item["matched"] else "<未匹配>"
        print(f"  {mark}  <-  {item['line']}")


if __name__ == "__main__":
    main()
