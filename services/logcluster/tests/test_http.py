"""HTTP 层：契约端点、鉴权、请求校验与错误码映射。"""

from __future__ import annotations

import json
from pathlib import Path

from src.state import STATE_SCHEMA_VERSION

from .conftest import build_client, build_client_with_engine, build_config

LINES = [
    "user alice logged in from 10.0.0.1",
    "user bob logged in from 10.0.0.2",
    "disk usage 95% on /dev/sda1",
]


def test_liveness_and_health_endpoints(client):
    assert client.get("/livez").json() == {"status": "ok"}

    for path in ("/healthz", "/health"):
        payload = client.get(path).json()
        assert payload["status"] == "ok"
        assert payload["models_loaded"] == ["default"]
        assert payload["uptime_secs"] >= 0
        assert payload["cluster_count"] == 0
        assert payload["total_size"] == 0


def test_readiness_is_ok_when_state_is_usable(client):
    payload = client.get("/readyz")
    assert payload.status_code == 200
    assert payload.json() == {"status": "ready", "profile": "default"}


def test_version_reports_effective_config(client):
    payload = client.get("/version").json()
    assert payload["name"] == "modelman-logcluster"
    assert payload["version"]
    assert payload["git_commit"]
    assert payload["profile_id"] == "default"
    assert payload["sim_th"] == 0.4
    assert payload["max_concurrency"] == 2
    assert payload["max_lines"] == 2000
    assert payload["auth_required"] is False
    assert payload["state_loaded"] is False


def test_models_reports_the_single_profile(client):
    [model] = client.get("/models").json()
    assert model["id"] == "default"
    assert model["available"] is True
    assert model["loaded"] is True
    assert model["load_error"] is None
    assert model["params"]["sim_th"] == 0.4
    assert model["state"]["schema_version"] == STATE_SCHEMA_VERSION
    assert model["state"]["loaded"] is False
    assert model["state"]["clusters"] == 0


def test_metrics_endpoint_serves_prometheus_text(client):
    client.post("/cluster", json={"lines": LINES})
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "logcluster_up 1" in body
    assert 'logcluster_requests_total{endpoint="cluster",status="ok"} 1' in body
    assert "logcluster_clusters 2" in body


def test_cluster_endpoint_keeps_the_existing_response_shape(client):
    response = client.post("/cluster", json={"lines": LINES})
    assert response.status_code == 200
    payload = response.json()

    assert payload["success"] is True
    assert payload["error"] is None
    assert payload["cluster_count"] == 2
    assert payload["time_ms"] >= 0
    assert [item["line"] for item in payload["results"]] == LINES
    # 第一条日志新建了簇，它的 template 就是这一行本身，这是 drain3 的语义
    assert payload["results"][0]["template"] == LINES[0]
    assert payload["results"][0]["parameters"] == []
    assert payload["results"][1]["template"] == "user <*> logged in from <*>"
    assert payload["results"][1]["change_type"] == "cluster_template_changed"


def test_match_endpoint_does_not_learn(client):
    client.post("/cluster", json={"lines": LINES})
    payload = client.post(
        "/match", json={"lines": ["user carol logged in from 10.0.0.9"]}
    ).json()
    assert payload["success"] is True
    assert payload["results"][0]["matched"] is True
    assert payload["results"][0]["template"] == "user <*> logged in from <*>"

    listing = client.get("/clusters").json()
    assert len(listing["clusters"]) == 2
    assert listing["total_size"] == 3


def test_clusters_endpoint_lists_templates(client):
    client.post("/cluster", json={"lines": LINES})
    payload = client.get("/clusters").json()
    assert payload["success"] is True
    assert payload["total_size"] == 3
    # 按 cluster_id 排序：先建的 user 模板有 2 条，后建的 disk 模板有 1 条。
    # 只有一个成员的簇没有发生过合并，模板就是那一行本身。
    assert [item["size"] for item in payload["clusters"]] == [2, 1]
    assert [item["template"] for item in payload["clusters"]] == [
        "user <*> logged in from <*>",
        LINES[2],
    ]


def test_validation_errors_are_400_with_an_error_field(client):
    response = client.post("/cluster", json={"lines": []})
    assert response.status_code == 400
    payload = response.json()
    assert payload["success"] is False
    assert "lines" in payload["error"]
    # detail 是旧实现的字段名，必须一起保留
    assert payload["detail"] == payload["error"]

    assert client.post("/cluster", json={"lines": "not-a-list"}).status_code == 400
    assert client.post("/match", json={}).status_code == 400


def test_line_count_limit(tmp_path):
    client = build_client(build_config(tmp_path, MAX_LINES="2"))
    response = client.post("/cluster", json={"lines": LINES})
    assert response.status_code == 400
    assert "MAX_LINES=2" in response.json()["error"]


def test_line_length_limit(tmp_path):
    client = build_client(build_config(tmp_path, MAX_LINE_CHARS="10"))
    response = client.post("/cluster", json={"lines": ["x" * 11]})
    assert response.status_code == 400
    assert "MAX_LINE_CHARS=10" in response.json()["error"]


def test_body_size_limit(tmp_path):
    client = build_client(build_config(tmp_path, MAX_BYTES="64"))
    response = client.post("/cluster", json={"lines": ["x" * 100]})
    assert response.status_code == 400
    assert "MAX_BYTES=64" in response.json()["error"]


def test_auth_token_gates_business_endpoints_and_metrics(tmp_path):
    client = build_client(build_config(tmp_path, AUTH_TOKEN="s3cret"))

    guarded = [
        ("post", "/cluster", {"json": {"lines": LINES}}),
        ("post", "/match", {"json": {"lines": LINES}}),
        ("get", "/clusters", {}),
        ("get", "/metrics", {}),
        ("get", "/openapi.json", {}),
    ]
    for method, path, kwargs in guarded:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, path
        assert response.json()["error"] == "missing or invalid auth token"

    assert (
        client.post(
            "/cluster", json={"lines": LINES}, headers={"X-Auth-Token": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/cluster", json={"lines": LINES}, headers={"X-Auth-Token": "s3cret"}
        ).status_code
        == 200
    )
    assert (
        client.get("/clusters", headers={"Authorization": "Bearer s3cret"}).status_code
        == 200
    )

    # 探活与状态端点保持免鉴权，容器在拿到凭据之前也能过健康检查
    for path in ("/livez", "/healthz", "/readyz", "/version", "/models"):
        assert client.get(path).status_code == 200, path


def test_openapi_is_available_but_guarded(tmp_path):
    client = build_client(build_config(tmp_path, AUTH_TOKEN="s3cret"))
    payload = client.get("/openapi.json", headers={"X-Auth-Token": "s3cret"}).json()
    assert "/cluster" in payload["paths"]
    # 交互式文档刻意关掉
    assert client.get("/docs").status_code == 404


def test_overload_maps_to_503(tmp_path):
    config = build_config(tmp_path, MAX_CONCURRENCY="1", QUEUE_TIMEOUT_SECS="0.01")
    client, engine = build_client_with_engine(config)

    engine._slots.acquire()
    try:
        response = client.post("/cluster", json={"lines": LINES})
    finally:
        engine._slots.release()

    assert response.status_code == 503
    assert "服务忙" in response.json()["error"]


def test_unavailable_state_is_visible_over_http(tmp_path):
    config = build_config(tmp_path)
    state_path = Path(config.state_dir) / "drain_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"schema_version": STATE_SCHEMA_VERSION + 1}))

    client = build_client(config)

    assert client.get("/healthz").json()["models_loaded"] == []
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert "schema_version" in ready.json()["error"]

    [model] = client.get("/models").json()
    assert model["loaded"] is False
    assert "schema_version" in model["load_error"]

    cluster = client.post("/cluster", json={"lines": LINES})
    assert cluster.status_code == 503
    assert "schema_version" in cluster.json()["error"]


def test_unknown_route_returns_a_json_error(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]
