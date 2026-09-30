"""HTTP 层测试：探活、状态、鉴权、错误码映射、体积上限。

不带权重的部分（`test_*`）跑得很快；带权重的部分（`test_forecast_*`）标了
`weights`，本地没下权重时会跳过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    build_client,
    build_client_loaded,
    build_config,
    requires_weights,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SERIES = [round(100 + 1.5 * i + 2.0 * (i % 5), 3) for i in range(36)]


# ─── 不加载权重 ─────────────────────────────────────────────────────────────


def test_livez(client: TestClient) -> None:
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_no_models_when_not_loaded(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["models_loaded"] == []
    assert body["uptime_secs"] >= 0


def test_readyz_is_503_before_weights(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    # 未就绪也必须给出可读原因
    assert body["error"]


def test_version_exposes_effective_config(client: TestClient) -> None:
    body = client.get("/version").json()
    assert body["name"] == "modelman-forecast"
    assert body["tier"] == "timesfm-3.0"
    assert body["max_concurrency"] == 2
    assert body["auth_required"] is False


def test_models_lists_the_tier_unloaded(client: TestClient) -> None:
    body = client.get("/models").json()
    assert len(body) == 1
    assert body[0]["id"] == "timesfm-3.0"
    assert body[0]["available"] is True
    assert body[0]["loaded"] is False
    assert body[0]["params"]["tier"] == "timesfm-3.0"


def test_load_failure_is_reported_not_fatal(tmp_path) -> None:
    # 权重缺失时进程照常起来，原因出现在 /models 与 /readyz 上
    config = build_config(tmp_path, PRELOAD="true")
    client = build_client(config)

    assert client.get("/livez").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503

    model = client.get("/models").json()[0]
    assert model["loaded"] is False
    assert model["load_error"], "/models 必须给出加载失败的原因"

    response = client.post(
        "/v1/forecast", json={"series": [{"values": SERIES}], "horizon": 4}
    )
    assert response.status_code == 503
    assert response.json()["error"]


def test_index_page_carries_the_ui(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<title>" in response.text
    # 页面免鉴权是刻意的，但必须带上与其它服务同款的安全头
    assert response.headers["content-security-policy"].startswith("default-src 'none'")


def test_static_assets_served(client: TestClient) -> None:
    assert client.get("/app.css").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_docs_endpoints_are_closed(client: TestClient) -> None:
    # FastAPI 自带的文档页关掉，只留一个（需鉴权的）openapi.json
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 200


def test_openapi_requires_auth_when_token_is_set(tmp_path) -> None:
    client = build_client(build_config(tmp_path, AUTH_TOKEN="s3cret"))
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers={"x-auth-token": "s3cret"}).status_code == 200


def test_metrics_requires_auth_when_token_is_set(tmp_path) -> None:
    config = build_config(tmp_path, AUTH_TOKEN="s3cret")
    client = build_client(config)

    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"x-auth-token": "wrong"}).status_code == 401
    response = client.get("/metrics", headers={"x-auth-token": "s3cret"})
    assert response.status_code == 200
    assert "forecast_up 1" in response.text
    # 探活端点刻意不鉴权：容器要能在注入凭据之前通过探活
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 503


def test_bearer_token_also_accepted(tmp_path) -> None:
    client = build_client(build_config(tmp_path, AUTH_TOKEN="s3cret"))
    response = client.get("/metrics", headers={"authorization": "Bearer s3cret"})
    assert response.status_code == 200


def test_forecast_requires_auth_when_token_is_set(tmp_path) -> None:
    client = build_client(build_config(tmp_path, AUTH_TOKEN="s3cret"))
    response = client.post("/v1/forecast", json={"series": [{"values": SERIES}], "horizon": 4})
    assert response.status_code == 401


@pytest.mark.parametrize(
    "payload,needle",
    [
        ({"horizon": 4}, "series"),
        ({"series": [], "horizon": 4}, "series"),
        ({"series": [{"values": SERIES}]}, "horizon"),
        ({"series": [{"values": SERIES}], "horizon": 0}, "horizon"),
        ({"series": [{"values": []}], "horizon": 4}, "values"),
        ({"series": [{"values": SERIES}], "horizon": 4, "quantiles": [1.5]}, "分位"),
    ],
)
def test_bad_requests_are_400_with_reason(client: TestClient, payload: dict, needle: str) -> None:
    response = client.post("/v1/forecast", json=payload)
    # 仓库约定：格式错误是 400，不是 FastAPI 默认的 422
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]
    assert needle in body["error"]


def test_horizon_over_budget_is_400(tmp_path) -> None:
    client = build_client(build_config(tmp_path, MAX_HORIZON="8"))
    response = client.post(
        "/v1/forecast", json={"series": [{"values": SERIES}], "horizon": 9}
    )
    assert response.status_code == 400
    assert "MAX_HORIZON=8" in response.json()["error"]


def test_too_many_series_is_400(tmp_path) -> None:
    client = build_client(build_config(tmp_path, MAX_SERIES="1"))
    response = client.post(
        "/v1/forecast",
        json={"series": [{"values": SERIES}, {"values": SERIES}], "horizon": 4},
    )
    assert response.status_code == 400
    assert "MAX_SERIES=1" in response.json()["error"]


def test_body_over_max_bytes_is_400(tmp_path) -> None:
    # 上限压到比任何合法请求都小，验的是中间件而不是路由
    client = build_client(build_config(tmp_path, MAX_BYTES="32"))
    response = client.post(
        "/v1/forecast", json={"series": [{"values": SERIES}], "horizon": 4}
    )
    assert response.status_code == 400
    assert "MAX_BYTES" in response.json()["error"]


def test_unknown_path_is_json_error(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]


# ─── 加载真实权重 ───────────────────────────────────────────────────────────


@pytest.mark.weights
@requires_weights
def test_forecast_end_to_end(tmp_path) -> None:
    client, engine = build_client_loaded(tmp_path)
    assert engine.is_ready(), engine.load_error

    response = client.post(
        "/v1/forecast", json={"series": [{"values": SERIES}], "horizon": 12}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["success"] is True
    assert body["model"] == "timesfm-3.0"
    assert body["horizon"] == 12
    assert body["context_lengths"] == [36]
    assert len(body["quantiles"]) == 9

    item = body["forecasts"][0]
    assert len(item["point"]) == 12
    assert sorted(item["quantiles"]) == sorted(repr(level) for level in body["quantiles"])

    # 分位单调性
    ordered = sorted(item["quantiles"], key=float)
    for step in range(12):
        column = [item["quantiles"][key][step] for key in ordered]
        assert column == sorted(column), (step, column)

    # 0.5 分位即点预测
    assert item["quantiles"]["0.5"] == item["point"]

    # 上升趋势的序列，12 步外推不该反向
    assert item["point"][-1] > item["point"][0]


@pytest.mark.weights
@requires_weights
def test_forecast_subset_of_quantiles(tmp_path) -> None:
    client, _engine = build_client_loaded(tmp_path)
    response = client.post(
        "/v1/forecast",
        json={
            "series": [{"values": SERIES}],
            "horizon": 6,
            "quantiles": [0.1, 0.9, 0.1],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quantiles"] == [0.1, 0.9]
    assert sorted(body["forecasts"][0]["quantiles"]) == ["0.1", "0.9"]


@pytest.mark.weights
@requires_weights
def test_forecast_rejects_unknown_quantile_after_load(tmp_path) -> None:
    client, engine = build_client_loaded(tmp_path)
    if 0.05 in engine.levels:
        pytest.skip("模型自带 0.05 分位，这条用不上")
    response = client.post(
        "/v1/forecast",
        json={"series": [{"values": SERIES}], "horizon": 4, "quantiles": [0.05]},
    )
    assert response.status_code == 400
    assert "不在模型自带的分位里" in response.json()["error"]


@pytest.mark.weights
@requires_weights
def test_forecast_batch_and_metrics(tmp_path) -> None:
    client, _engine = build_client_loaded(tmp_path)
    response = client.post(
        "/v1/forecast",
        json={
            "series": [
                {"id": "a", "values": SERIES},
                {"id": "b", "values": [value * 2 for value in SERIES]},
            ],
            "horizon": 6,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["forecasts"]] == ["a", "b"]
    assert body["usage"]["series"] == 2
    assert body["usage"]["horizon_points"] == 12

    metrics = client.get("/metrics").text
    assert 'forecast_requests_total{endpoint="forecast",status="ok"} 1' in metrics
    assert "forecast_model_ready 1" in metrics
    assert "forecast_series_total 2" in metrics


@pytest.mark.weights
@requires_weights
def test_readyz_after_load(tmp_path) -> None:
    client, engine = build_client_loaded(tmp_path)
    assert engine.is_ready()
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_baseline_file_is_valid_json(baseline: dict) -> None:
    # 基线必须存在且能被解析：契约测试的输入之一
    assert baseline["cases"], "基线里没有样本"
    assert json.dumps(baseline)
