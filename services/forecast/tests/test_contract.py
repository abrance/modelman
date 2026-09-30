"""契约测试：逐样本比对基线，并断言输出形状与分位单调性。

基线由 `make fixtures SERVICE=forecast` 生成，提交前必须人工确认结果合理。
断言分三层：

1. **形状**：步长、分位个数、变量数必须与基线一致——形状变了说明引擎的取轴错了。
2. **数值**：点预测与分位预测按 tolerance 比对。
3. **单调性**：同一时刻，分位越高预测值不能越小（模型带 sort_quantiles，
   所以这条本该恒成立；它防的是我们自己在取轴或拼装时分位错位）。

再加汇总层的延迟预算，只用来捕捉数量级退化。
"""

from __future__ import annotations

import statistics

import pytest

from tests.conftest import (
    MODELS_DIR,
    build_engine,
    loaded_config,
    requires_weights,
)

pytestmark = [pytest.mark.weights, requires_weights]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    config = loaded_config(tmp_path_factory.mktemp("contract"))
    engine, _metrics = build_engine(config)
    assert engine.is_ready(), engine.load_error
    return engine


def test_baseline_matches_registry(baseline: dict, digest_payload: dict) -> None:
    # 基线里的权重标识必须与 registry 一致：改权重必须重新生成基线
    assert baseline["model"]["revision"] == digest_payload["revision"]
    assert baseline["model"]["files"] == digest_payload["files"]
    assert baseline["model"]["tier"] == "timesfm-3.0"


def test_baseline_params_match_config(baseline: dict, tmp_path) -> None:
    config = loaded_config(tmp_path)
    assert baseline["params"] == config.profile()


def test_models_dir_is_where_we_think(digest_payload: dict) -> None:
    for name in digest_payload["files"]:
        assert (MODELS_DIR / "timesfm-3.0" / name).is_file(), name


def test_levels_match_the_model(engine, baseline: dict) -> None:
    # 分位水平来自权重自带的 config，不是我们写死的
    assert engine.levels == baseline["cases"][0]["levels"]


def test_each_case_matches_baseline(engine, cases, baseline) -> None:
    tolerance = baseline["tolerance"]
    expected_by_id = {item["id"]: item for item in baseline["cases"]}
    latencies = []

    for case in cases:
        expected = expected_by_id[case["id"]]
        request = _request(case)
        series, _truncated = _prepare(request, engine)
        levels = [float(level) for level in expected["levels"]]

        outcome = engine.forecast(series, request.horizon, levels)
        latencies.append(outcome.elapsed_ms)

        assert len(outcome.items) == len(expected["forecasts"]), case["id"]
        for got, want in zip(outcome.items, expected["forecasts"]):
            assert got.id == want["id"], case["id"]

            point = got.point
            assert point.shape == (request.horizon,), (case["id"], point.shape)
            _close(point, want["point"], tolerance, case["id"], "point")

            # 形状：分位个数与基线一致
            assert sorted(got.quantiles) == sorted(want["quantiles"]), case["id"]

            # 单调性：按分位从小到大逐个比较
            ordered = sorted(got.quantiles, key=float)
            matrix = [got.quantiles[key] for key in ordered]
            for step in range(request.horizon):
                column = [float(matrix[index][step]) for index in range(len(ordered))]
                assert column == sorted(column), (
                    f"{case['id']} 第 {step} 步分位不单调：{column}"
                )

            # 0.5 分位就是点预测
            if "0.5" in got.quantiles:
                assert (got.quantiles["0.5"] == point).all(), case["id"]

            for level, values in want["quantiles"].items():
                _close(got.quantiles[level], values, tolerance, case["id"], f"q{level}")

    median = statistics.median(latencies)
    assert median <= baseline["latency_budget_ms"], (
        f"单样本中位延迟 {median:.0f} ms 超过预算 {baseline['latency_budget_ms']} ms"
    )


def test_batch_request_keeps_series_aligned(engine, cases, baseline) -> None:
    case = next(item for item in cases if item["id"] == "case_batch")
    request = _request(case)
    series, _ = _prepare(request, engine)
    expected = next(item for item in baseline["cases"] if item["id"] == "case_batch")

    outcome = engine.forecast(series, request.horizon, [float(x) for x in expected["levels"]])
    assert [item.id for item in outcome.items] == ["up", "down"]
    # 一条上升一条下降，点预测的走势不能反过来
    up = outcome.items[0].point
    down = outcome.items[1].point
    assert up[-1] > up[0] - 1e-3
    assert down[-1] < down[0] + 1e-3


def test_repeated_calls_are_deterministic(engine, cases) -> None:
    case = next(item for item in cases if item["id"] == "case_trend")
    request = _request(case)
    series, _ = _prepare(request, engine)
    levels = engine.levels

    first = engine.forecast(series, request.horizon, levels)
    second = engine.forecast(series, request.horizon, levels)
    assert (first.items[0].point == second.items[0].point).all()
    for level in levels:
        key = repr(level)
        assert (first.items[0].quantiles[key] == second.items[0].quantiles[key]).all()


# ─── 辅助 ───────────────────────────────────────────────────────────────────


def _request(case: dict):
    from src.models import ForecastRequest, Series

    return ForecastRequest(
        series=[Series(**item) for item in case["series"]],
        horizon=case["horizon"],
        quantiles=case.get("quantiles"),
    )


def _prepare(request, engine):
    from src.engine import prepare_series, resolve_levels, validate_request

    validate_request(request, engine.config)
    series, truncated = prepare_series(request.series, engine.config)
    resolve_levels(request, engine.levels)
    return series, truncated


def _close(got, want, tolerance: float, case_id: str, label: str) -> None:
    assert len(got) == len(want), (case_id, label, len(got), len(want))
    for index, value in enumerate(want):
        assert abs(float(got[index]) - float(value)) <= tolerance, (
            f"{case_id} {label}[{index}]：基线 {value}，实际 {float(got[index])}"
        )
