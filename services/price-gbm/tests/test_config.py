"""配置解析测试：默认值开箱可跑、非法值退出码口径、bool 解析。"""

from __future__ import annotations

import pytest

from src.config import Config, ConfigError


def test_defaults_dev_machine_ready() -> None:
    config = Config.from_env({})
    assert config.model_tier == "gbm-hour-v1"
    assert config.max_days == 60
    assert config.max_horizon_days == 14
    # 开发机缺省必须能直接跑：models 目录指向服务目录
    assert config.models_dir.is_dir()


def test_listen_addr_parse() -> None:
    assert Config.from_env({"LISTEN_ADDR": "0.0.0.0:9123"}).host_port == ("0.0.0.0", 9123)
    with pytest.raises(ConfigError):
        Config.from_env({"LISTEN_ADDR": "no-port"}).host_port
    with pytest.raises(ConfigError):
        Config.from_env({"LISTEN_ADDR": "abc:notdigid"}).host_port


def test_int_env_validation() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"MAX_DAYS": "ten"})
    assert Config.from_env({"MAX_DAYS": "80"}).max_days == 80


def test_bool_env() -> None:
    assert Config.from_env({"VERIFY_WEIGHTS": "false"}).verify_weights is False
    assert Config.from_env({"VERIFY_WEIGHTS": "TRUE"}).verify_weights is True
    # AUTH_TOKEN 空白视为未启用
    assert Config.from_env({"AUTH_TOKEN": "  "}).auth_token is None
    assert Config.from_env({"AUTH_TOKEN": " s3cret "}).auth_token == "s3cret"
