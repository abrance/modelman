"""HTTP 层测试：契约端点、鉴权、错误码、体积上限与真实判定。

不带权重的用例用 `PRELOAD=false` 的配置构造 app：进程没就绪时探活端点照常
200、业务端点 503，这正是部署门禁要依赖的行为，值得单独测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import (
    build_client,
    build_config,
    build_client_loaded,
    requires_weights,
)

CHOICE = {
    "state": {"prompt": "查一下 k8s 里 pod 为什么一直 pending"},
    "questions": {
        "route": {
            "type": "choice",
            "instructions": "pick one",
            "criteria": {"bash": "shell", "kubectl": "kubernetes"},
        }
    },
}


def test_livez_is_open_without_weights(client) -> None:
    assert client.get("/livez").json() == {"status": "ok"}


def test_healthz_reports_no_model_when_not_loaded(client) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["models_loaded"] == []


def test_readyz_is_503_without_weights(client) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["error"]


def test_version_exposes_effective_config(client) -> None:
    body = client.get("/version").json()
    assert body["name"] == "modelman-jev"
    assert body["tier"] == "multilingual"
    assert body["max_len"] == 1024
    assert body["auth_required"] is False


def test_models_explains_missing_weights(client) -> None:
    """`PRELOAD=false` 时是"还没加载"，不是错误：load_error 为空，loaded 为假。"""
    body = client.get("/models").json()
    assert len(body) == 1
    assert body[0]["id"] == "multilingual"
    assert body[0]["loaded"] is False
    assert body[0]["load_error"] is None
    assert body[0]["params"]["tier"] == "multilingual"


def test_metrics_is_prometheus_text(client) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "jev_up 1" in response.text
    assert "jev_model_ready 0" in response.text


def test_ui_is_served_with_csp(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "<title>" in response.text


def test_ui_assets_are_served(client) -> None:
    assert "--accent" in client.get("/app.css").text
    assert "Authorization" in client.get("/app.js").text


def test_docs_are_disabled(client) -> None:
    assert client.get("/docs").status_code == 404


def test_openapi_requires_auth_when_configured(tmp_path: Path) -> None:
    client = build_client(build_config(tmp_path, AUTH_TOKEN="secret"))
    assert client.get("/openapi.json").status_code == 401
    ok = client.get("/openapi.json", headers={"X-Auth-Token": "secret"})
    assert ok.status_code == 200


def test_business_endpoint_is_503_without_weights(client) -> None:
    response = client.post("/v1/systemone", json=CHOICE)
    assert response.status_code == 503
    assert response.json()["error"]


def test_invalid_body_is_400_not_422(client) -> None:
    response = client.post("/v1/systemone", json={"state": "x", "questions": {}})
    assert response.status_code == 400
    assert response.json()["error"]


def test_auth_is_enforced_for_business_endpoint(tmp_path: Path) -> None:
    client = build_client(build_config(tmp_path, AUTH_TOKEN="secret"))
    assert client.post("/v1/systemone", json=CHOICE).status_code == 401
    bad = client.post(
        "/v1/systemone", json=CHOICE, headers={"Authorization": "Bearer wrong"}
    )
    assert bad.status_code == 401
    # 探活端点不带鉴权，这样容器能在注入凭据之前通过探活
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 503
    assert (
        client.post(
            "/v1/systemone",
            json=CHOICE,
            headers={"Authorization": "Bearer secret"},
        ).status_code
        == 503
    )


def test_body_size_limit_is_enforced(tmp_path: Path) -> None:
    client = build_client(build_config(tmp_path, MAX_BYTES=1024, AUTH_TOKEN="secret"))
    payload = dict(CHOICE)
    payload["state"] = "x" * 4096
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/v1/systemone", json=payload, headers=headers).status_code == 400


@requires_weights
def test_systemone_returns_jev_shaped_answers(tmp_path: Path) -> None:
    client, _ = build_client_loaded(tmp_path)
    assert client.get("/readyz").status_code == 200

    response = client.post("/v1/systemone", json=CHOICE)
    assert response.status_code == 200
    body = response.json()
    answer = body["answers"]["route"]
    assert answer["choice"] in CHOICE["questions"]["route"]["criteria"]
    assert "distribution" in answer
    assert body["model"] == "multilingual"
    assert body["time_ms"] > 0
    assert body["usage"]["totalTokens"] >= body["usage"]["input_tokens"]


@requires_weights
def test_systemone_accepts_bearer_token(tmp_path: Path) -> None:
    client, _ = build_client_loaded(tmp_path, AUTH_TOKEN="secret")
    assert client.post("/v1/systemone", json=CHOICE).status_code == 401
    ok = client.post(
        "/v1/systemone", json=CHOICE, headers={"Authorization": "Bearer secret"}
    )
    assert ok.status_code == 200


@requires_weights
def test_metrics_counts_a_real_request(tmp_path: Path) -> None:
    client, _ = build_client_loaded(tmp_path)
    client.post("/v1/systemone", json=CHOICE)
    text = client.get("/metrics").text
    assert 'jev_requests_total{endpoint="systemone",status="ok"} 1' in text
    assert "jev_model_ready 1" in text
    assert "jev_process_resident_memory_bytes" in text


@requires_weights
def test_models_reports_digests_and_load_time(tmp_path: Path) -> None:
    client, _ = build_client_loaded(tmp_path)
    state = client.get("/models").json()[0]["state"]
    assert state["digest_verified"] is True
    assert state["digests"] >= 2
    assert state["load_seconds"] > 0
    assert state["weights_bytes"] > 600_000_000


@requires_weights
def test_ui_can_drive_the_endpoint(tmp_path: Path) -> None:
    """页面上的调用形状与契约一致：这条用例保证页面不会悄悄脱离后端。"""
    client, _ = build_client_loaded(tmp_path)
    page = client.get("/app.js").text
    assert "/v1/systemone" in page
    response = client.post("/v1/systemone", json=CHOICE)
    assert json.loads(response.text)["success"] is True
