"""请求整形的单元测试。不加载权重：跑的是 prepare_series / resolve_levels。"""

from __future__ import annotations


from types import SimpleNamespace

import numpy as np
import pytest

from src.config import Config
from src.engine import (
    InvalidRequest,
    level_key,
    prepare_series,
    resolve_levels,
    validate_request,
)
from src.models import ForecastRequest, Series


def make_request(**overrides) -> ForecastRequest:
    payload = {
        "series": [{"values": [1.0, 2.0, 3.0, 4.0]}],
        "horizon": 4,
    }
    payload.update(overrides)
    return ForecastRequest(**payload)


def series_of(*value_lists) -> list:
    return [Series(values=list(values)) for values in value_lists]


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.from_env({"MODELS_DIR": str(tmp_path / "models")})


def test_single_variate_is_one_dimensional(cfg: Config) -> None:
    prepared, truncated = prepare_series(series_of([1, 2, 3]), cfg)
    assert truncated is False
    assert len(prepared) == 1
    assert prepared[0][1].shape == (3,)
    assert prepared[0][1].dtype == np.float32


def test_multivariate_keeps_variate_axis(cfg: Config) -> None:
    prepared, _ = prepare_series(series_of([[1, 2, 3], [4, 5, 6]]), cfg)
    assert prepared[0][1].shape == (2, 3)


def test_ids_survive(cfg: Config) -> None:
    prepared, _ = prepare_series(
        [Series(id="a", values=[1.0, 2.0]), Series(values=[3.0, 4.0])], cfg
    )
    assert [ts_id for ts_id, _ in prepared] == ["a", None]


def test_long_context_is_truncated_to_the_tail(cfg: Config) -> None:
    values = list(range(20))
    config = Config.from_env({"MODELS_DIR": str(cfg.models_dir), "MAX_CONTEXT": "5"})
    prepared, truncated = prepare_series(series_of(values), config)
    assert truncated is True
    # 取最后一段：时序里越近的点越重要
    assert prepared[0][1].tolist() == [15, 16, 17, 18, 19]


def test_mixed_shapes_rejected(cfg: Config) -> None:
    with pytest.raises(InvalidRequest) as excinfo:
        prepare_series(
            [Series(values=[1.0, 2.0]), Series(values=[[1.0, 2.0], [3.0, 4.0]])], cfg
        )
    assert "形状与 series[0] 不一致" in str(excinfo.value)


def test_ragged_variates_rejected(cfg: Config) -> None:
    with pytest.raises(InvalidRequest) as excinfo:
        prepare_series(series_of([[1.0, 2.0, 3.0], [4.0, 5.0]]), cfg)
    assert "不一致" in str(excinfo.value)


def test_variate_count_must_match_across_series(cfg: Config) -> None:
    with pytest.raises(InvalidRequest) as excinfo:
        prepare_series(
            series_of([[1.0, 2.0], [3.0, 4.0]], [[1.0, 2.0]]),
            cfg,
        )
    assert "series[" in str(excinfo.value)


def test_too_many_series_rejected(cfg: Config) -> None:
    config = Config.from_env({"MODELS_DIR": str(cfg.models_dir), "MAX_SERIES": "2"})
    with pytest.raises(InvalidRequest) as excinfo:
        prepare_series(series_of([1, 2], [3, 4], [5, 6]), config)
    assert "MAX_SERIES=2" in str(excinfo.value)


def test_empty_values_rejected(cfg: Config) -> None:
    # pydantic 层已经拦住空数组，这里测的是 prepare_series 自己的防线
    with pytest.raises(InvalidRequest):
        prepare_series([SimpleNamespace(id=None, values=[])], cfg)


def test_series_model_rejects_empty_values() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Series(values=[])


def test_non_finite_rejected(cfg: Config) -> None:
    with pytest.raises(InvalidRequest):
        prepare_series([Series(values=[1.0, float("inf")])], cfg)


def test_horizon_over_budget_rejected(cfg: Config) -> None:
    config = Config.from_env({"MODELS_DIR": str(cfg.models_dir), "MAX_HORIZON": "8"})
    with pytest.raises(InvalidRequest) as excinfo:
        validate_request(make_request(horizon=9), config)
    assert "MAX_HORIZON=8" in str(excinfo.value)


@pytest.mark.parametrize("level", [0.0, 1.0, -0.1, 1.5])
def test_quantile_levels_must_be_inside_open_interval(cfg: Config, level: float) -> None:
    with pytest.raises(InvalidRequest):
        validate_request(make_request(quantiles=[level]), cfg)


def test_empty_quantile_list_rejected(cfg: Config) -> None:
    with pytest.raises(InvalidRequest) as excinfo:
        validate_request(make_request(quantiles=[]), cfg)
    assert "不能是空列表" in str(excinfo.value)


def test_levels_default_to_model_quantiles() -> None:
    available = [0.1, 0.5, 0.9]
    assert resolve_levels(make_request(), available) == available


def test_requested_levels_are_deduped_and_sorted() -> None:
    available = [0.1, 0.5, 0.9]
    assert resolve_levels(make_request(quantiles=[0.9, 0.5, 0.9]), available) == [0.5, 0.9]


def test_level_key_has_no_float_noise() -> None:
    assert level_key(0.1) == "0.1"
    assert level_key(0.3) == "0.3"
