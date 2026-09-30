#!/usr/bin/env python3
"""容器冒烟检查：起容器、等就绪、跑一次真实判定、报告健康状态。

输入（环境变量）：
  IMAGE  镜像全名，含 tag
  PORT   宿主机端口，映射到容器 8080，默认 8080
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 10.0


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
    with urllib.request.urlopen(request, timeout=60.0) as response:
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
    )

    # 自带页面随镜像交付，确认真的带上去了
    status, raw = get(port, "/")
    assert b"<title>" in raw, "页面没有标题"
    status, raw = get(port, "/app.js")
    assert b"X-Auth-Token" in raw or b"Authorization" in raw
    print("smoke ui ok")

    # 真实判定一次：中文工具路由，三个问题一次前向
    payload = {
        "state": {
            "prompt": "查一下 k8s 里 pod 为什么一直 pending",
            "tools": [
                "bash: Execute bash commands in the current working directory",
                "kubectl_describe: Show details of a Kubernetes resource or pod",
                "ocr_image: Extract text from an image using the NAS OCR service",
            ],
        },
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Which single tool should handle this request?",
                "criteria": {
                    "bash": "shell commands",
                    "kubectl_describe": "kubernetes resource inspection",
                    "ocr_image": "read text off an image",
                    "unrelated": "no listed tool fits",
                },
            },
            "needed": {
                "type": "noul",
                "instructions": "Is any listed tool needed for this request?",
            },
            "complexity": {
                "type": "score",
                "instructions": "How complex is this request?",
                "criteria": ["trivial", "moderate", "complex"],
            },
        },
    }
    status, body = post(port, "/v1/systemone", payload, token)
    assert status == 200, status
    answers = body["answers"]
    assert answers["route"]["choice"] in payload["questions"]["route"]["criteria"], answers
    assert 0.0 <= float(answers["needed"]["noul"]) <= 1.0, answers
    assert "distribution" in answers["route"], "choice 答案缺少 distribution（Jev 客户端读这个键）"
    assert body["usage"]["totalTokens"] >= 0, body["usage"]
    print(
        "smoke systemone ok:",
        answers["route"]["choice"],
        "conf",
        round(float(answers["route"]["confidence"]), 3),
        "noul",
        round(float(answers["needed"]["noul"]), 3),
        "time_ms",
        body["time_ms"],
    )

    # 格式错的请求必须是 400，且带可读原因
    try:
        post(port, "/v1/systemone", {"state": "x", "questions": {"q": {"type": "bogus"}}}, token)
        raise AssertionError("非法问题类型没有被拒绝")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400, exc.code
        detail = json.loads(exc.read().decode("utf-8"))
        assert detail.get("error"), detail
        print("smoke error mapping ok:", detail["error"][:60])

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"smoke 断言失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
