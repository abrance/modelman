#!/usr/bin/env python3
"""冒烟断言：起容器之后打一次真实预测请求并核对响应结构。

只用标准库：镜像里没有 curl/jq，宿主机也不假设装了 requests。
用法：python tools/smoke_checks.py [PORT]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

PORT = sys.argv[1] if len(sys.argv) > 1 else "8080"
HISTORY_DAYS = 40
def main() -> int:
    cases_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cases.json"
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    case = payload["cases"][0]
    body = json.dumps({
        "days": case["days"],
        "target": case["target"],
    }).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/price-forecast",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read())

    problems = []
    if not result.get("success"):
        problems.append(f"success=false: {result}")
    point = result.get("point") or []
    if len(point) != 24:
        problems.append(f"point 有 {len(point)} 个点，应为 24")
    quantiles = result.get("quantiles") or {}
    for level in ("0.1", "0.5", "0.9"):
        if len(quantiles.get(level) or []) != 24:
            problems.append(f"quantiles[{level}] 不是 24 个点")
    q10, q50, q90 = quantiles.get("0.1") or [], quantiles.get("0.5") or [], quantiles.get("0.9") or []
    if q10 and q50 and q90:
        for hour in range(24):
            if not (q10[hour] <= q50[hour] <= q90[hour]):
                problems.append(f"{hour}:00 分位不单调")
    if (result.get("time_ms") or 0) > 2000:
        problems.append(f"耗时 {result.get('time_ms')}ms 超预算")

    if problems:
        print("smoke FAIL:", "；".join(problems), file=sys.stderr)
        return 1
    print(f"smoke ok: target={result['target']} horizon={result['horizon_days']}天 "
          f"耗时{result['time_ms']}ms 中位前3点={[round(x, 1) for x in point[:3]]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
