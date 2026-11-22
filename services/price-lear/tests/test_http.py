"""HTTP 层测试：端点约定、错误码、鉴权、页面。"""

from __future__ import annotations

from .conftest import build_client, build_config, fixture_cases, requires_models


@requires_models
def test_health_endpoints() -> None:
    client = build_client(build_config())
    assert client.get("/livez").json()["status"] == "ok"
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["models_loaded"] == ["lear-hour-v1"]
    assert client.get("/readyz").status_code == 200
    version = client.get("/version").json()
    assert version["name"] == "modelman-price-lear"
    models = client.get("/models").json()
    assert models[0]["id"] == "lear-hour-v1" and models[0]["loaded"]


@requires_models
def test_forecast_roundtrip() -> None:
    client = build_client(build_config())
    case = fixture_cases()[0]
    response = client.post("/v1/price-forecast", json=case)
    assert response.status_code == 200
    body = response.json()
    assert body["success"] and body["model"] == "lear-hour-v1"
    assert len(body["point"]) == 24
    assert body["horizon_days"] == 5


@requires_models
def test_error_mapping() -> None:
    client = build_client(build_config())
    case = fixture_cases()[0]
    # 400：目标日不晚于锚点
    bad = {**case, "target": case["days"][0]["date"]}
    assert client.post("/v1/price-forecast", json=bad).status_code == 400
    # 400：请求体结构错（FastAPI 校验折算成 400 而不是 422）
    assert client.post("/v1/price-forecast", json={"days": []}).status_code == 400
    assert "error" in client.post("/v1/price-forecast", json={"days": []}).json()


@requires_models
def test_auth_token_guards_business_endpoint() -> None:
    client = build_client(build_config(AUTH_TOKEN="s3cret"))
    assert client.post("/v1/price-forecast", json=fixture_cases()[0]).status_code == 401
    ok = client.post("/v1/price-forecast", json=fixture_cases()[0],
                     headers={"X-Auth-Token": "s3cret"})
    assert ok.status_code == 200
    # 健康端点刻意免鉴权
    assert client.get("/livez").status_code == 200
    # metrics 需要鉴权
    assert client.get("/metrics").status_code == 401


def test_ui_page_served_with_csp() -> None:
    client = build_client(build_config())
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "<title>" in response.text
