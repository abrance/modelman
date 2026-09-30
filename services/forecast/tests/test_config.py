"""配置解析的单元测试。不加载权重。"""

from __future__ import annotations

import pytest

from src.config import (
    MODEL_MAX_CONTEXT,
    Config,
    ConfigError,
    parse_listen_addr,
)


def test_defaults_are_dev_friendly() -> None:
    config = Config.from_env({})
    assert config.listen_addr == "0.0.0.0:8080"
    assert config.model_tier == "timesfm-3.0"
    assert config.device == "cpu"
    assert config.preload is True
    # 权重摘要校验默认打开：模型对损坏的 safetensors 不一定报错
    assert config.verify_weights is True
    assert config.max_concurrency == 2
    assert config.auth_token is None
    assert config.model_dir.name == "timesfm-3.0"


def test_default_threads_are_capped() -> None:
    # 部署主机的核数与邻居共享，默认线程数不该跟着核数走
    assert 1 <= Config.from_env({}).infer_threads <= 4


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"LISTEN_ADDR": "8080"}, "host:port"),
        ({"LISTEN_ADDR": "0.0.0.0:0"}, "0..65535"),
        ({"MODEL_TIER": "timesfm-2.5"}, "不是已知档位"),
        ({"DEVICE": "mps"}, "只支持 cpu 或 cuda"),
        ({"INFER_THREADS": "0"}, "必须 >= 1"),
        ({"MAX_CONTEXT": "0"}, "必须落在"),
        ({"MAX_CONTEXT": str(MODEL_MAX_CONTEXT + 1)}, "必须落在"),
        ({"MAX_HORIZON": "0"}, "必须 >= 1"),
        ({"MAX_SERIES": "0"}, "必须 >= 1"),
        ({"MAX_BYTES": "0"}, "必须 >= 1"),
        ({"MAX_CONCURRENCY": "0"}, "必须 >= 1"),
        ({"QUEUE_TIMEOUT_SECS": "0"}, "必须 > 0"),
        ({"VERIFY_WEIGHTS": "maybe"}, "不是布尔值"),
        ({"LOG_LEVEL": "loud"}, "不是合法的日志级别"),
    ],
)
def test_invalid_values_fail_fast(overrides: dict, message: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(overrides)
    assert message in str(excinfo.value)


def test_auth_token_blank_means_disabled() -> None:
    assert Config.from_env({"AUTH_TOKEN": "   "}).auth_token is None
    assert Config.from_env({"AUTH_TOKEN": "secret"}).auth_token == "secret"


def test_limit_concurrency_follows_max_concurrency() -> None:
    config = Config.from_env({"MAX_CONCURRENCY": "3"})
    assert config.limit_concurrency == 12


def test_profile_is_stable_for_registry_check() -> None:
    # registry/forecast.yaml 与契约基线都比对这个指纹，键顺序固定才算可逐字比较
    profile = Config.from_env({}).profile()
    assert list(profile) == ["tier", "device", "max_context", "max_horizon"]


@pytest.mark.parametrize(
    "value,expected",
    [("127.0.0.1:9102", ("127.0.0.1", 9102)), ("0.0.0.0:8080", ("0.0.0.0", 8080))],
)
def test_parse_listen_addr(value: str, expected: tuple) -> None:
    assert parse_listen_addr(value) == expected
