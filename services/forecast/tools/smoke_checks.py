#!/usr/bin/env python3
"""容器冒烟检查：打真实请求、断言形状与分位单调性、报告健康状态。

输入（环境变量）：
  PORT       宿主机端口，映射到容器 8080，默认 8080
  AUTH_TOKEN 服务启用鉴权时用它（与容器同一份）
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 10.0
FORECAST_TIMEOUT = 120.0


def get(port: int, path: str, token: str | None = None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status, response.read()


def post(port: int, path: str, payload: dict, token: str | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=FORECAST_TIMEOUT) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main() -> int:
    port = int(os.environ.get("PORT", "8080"))
    token = os.environ.get("AUTH_TOKEN") or None

    status, _ = get(port, "/livez")
    assert status == 200, status
    print("smoke livez ok")

    status, raw = get(port, "/version")
    version = json.loads(raw)
    print("smoke version:", version["version"], version["git_commit"], "tier", version["tier"])

    status, raw = get(port, "/models")
    model = json.loads(raw)[0]
    state = model["state"]
    assert state["digest_verified"] is True, state
    print(
        "smoke models:",
        model["id"],
        "weights_mb",
        round(state["weights_bytes"] / 1e6, 1),
        "digests",
        state["digests"],
        "load_s",
        state["load_seconds"],
        "global_context",
        state["global_context"],
        "quantiles",
        len(state["quantiles"]),
    )

    # 自带页面随镜像交付，确认真的带上去了
    status, raw = get(port, "/")
    assert b"<title>" in raw, "页面没有标题"
    status, raw = get(port, "/app.js")
    assert b"x-auth-token" in raw and b"/v1/forecast" in raw, "页面脚本里没有业务端点"
    print("smoke ui ok")

    # 真实预测一次：36 点上下文、12 步、全分位
    values = [round(100 + 1.5 * i + 2.0 * math.sin(i / 3.0), 3) for i in range(36)]
    status, body = post(port, "/v1/forecast", {"series": [{"values": values}], "horizon": 12}, token)
    assert status == 200, status
    assert body["success"] is True, body
    item = body["forecasts"][0]
    assert len(item["point"]) == 12, len(item["point"])
    assert body["context_lengths"] == [36], body["context_lengths"]
    assert len(body["quantiles"]) == 9, body["quantiles"]
    usage = body["usage"]
    assert usage["series"] == 1 and usage["horizon_points"] == 12, usage

    # 分位单调性：同一时刻，分位越高预测值不能越小。
    levels = sorted(body["quantiles"])
    for step in range(12):
        column = [item["quantiles"][repr(level)][step] for level in levels]
        assert column == sorted(column), f"第 {step} 步分位不单调：{column}"
    # 中位数点预测与 0.5 分位一致
    half = item["quantiles"][repr(0.5)]
    for step in range(12):
        assert abs(half[step] - item["point"][step]) < 1e-6, (step, half[step], item["point"][step])
    print(
        "smoke forecast ok: horizon",
        body["horizon"],
        "ctx",
        body["context_lengths"][0],
        "point0",
        round(item["point"][0], 2),
        "pxn0",
        round(item["quantiles"]["0.1"][0], 2),
        "pxx0",
        round(item["quantiles"]["0.9"][0], 2),
        "time_ms",
        body["time_ms"],
    )

    # 格式错的请求必须是 400，且带可读原因
    for bad, why in (
        ({"series": [{"values": [1, 2, 3]}], "horizon": 0}, "horizon=0"),
        ({"series": [{"values": []}], "horizon": 4}, "空序列"),
        ({"series": [{"values": [1, 2, 3]}], "horizon": 4, "quantiles": [0.05]}, "不支持的分位"),
    ):
        try:
            post(port, "/v1/forecast", bad, token)
            raise AssertionError(f"{why} 没有被拒绝")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, f"{why}: {exc.code}"
            detail = json.loads(exc.read().decode("utf-8"))
            assert detail.get("error"), detail
            print(f"smoke error mapping ok ({why}):", detail["error"][:70])

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"smoke 断言失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
