"""配置解析：默认值、校验、profile 指纹。"""

from __future__ import annotations

import pytest
from src.config import Config, ConfigError, parse_listen_addr

from .conftest import build_config


def test_defaults_are_usable(tmp_path):
    config = build_config(tmp_path)
    assert config.sim_th == 0.4
    assert config.depth == 4
    assert config.mask_rules == ()
    assert config.parametrize_numeric is True
    assert config.max_concurrency == 2
    assert config.queue_timeout_secs == 30.0
    assert config.max_lines == 2000
    assert config.auth_token is None
    assert config.host_port == ("0.0.0.0", 8080)


def test_limit_concurrency_follows_max_concurrency(tmp_path):
    assert build_config(tmp_path, MAX_CONCURRENCY="4").limit_concurrency == 16
    # 下限：并发很小时也要留出足够的连接余量
    assert build_config(tmp_path, MAX_CONCURRENCY="1").limit_concurrency == 8
    assert (
        build_config(
            tmp_path, MAX_CONCURRENCY="2", LIMIT_CONCURRENCY="3"
        ).limit_concurrency
        == 3
    )


def test_mask_rules_accept_list_and_empty(tmp_path):
    assert build_config(tmp_path, MASK_RULES="ip, uuid").mask_rules == ("ip", "uuid")
    assert build_config(tmp_path, MASK_RULES="").mask_rules == ()
    with pytest.raises(ConfigError, match="未知规则"):
        build_config(tmp_path, MASK_RULES="ip,nginx")


@pytest.mark.parametrize(
    "env, message",
    [
        ({"SIM_TH": "0"}, "SIM_TH"),
        ({"SIM_TH": "1.5"}, "SIM_TH"),
        ({"SIM_TH": "abc"}, "SIM_TH"),
        ({"DEPTH": "2"}, "DEPTH"),
        ({"MAX_CHILDREN": "1"}, "MAX_CHILDREN"),
        ({"MAX_CONCURRENCY": "0"}, "MAX_CONCURRENCY"),
        ({"QUEUE_TIMEOUT_SECS": "0"}, "QUEUE_TIMEOUT_SECS"),
        ({"MAX_LINES": "0"}, "MAX_LINES"),
        ({"MAX_LINE_CHARS": "0"}, "MAX_LINE_CHARS"),
        ({"MAX_BYTES": "0"}, "MAX_BYTES"),
        ({"SNAPSHOT_INTERVAL_MINUTES": "0"}, "SNAPSHOT_INTERVAL_MINUTES"),
        ({"PARAMETRIZE_NUMERIC": "maybe"}, "PARAMETRIZE_NUMERIC"),
        ({"LOG_LEVEL": "loud"}, "LOG_LEVEL"),
        ({"LISTEN_ADDR": "8080"}, "LISTEN_ADDR"),
        ({"LISTEN_ADDR": "0.0.0.0:notaport"}, "LISTEN_ADDR"),
        ({"LISTEN_ADDR": "0.0.0.0:99999"}, "LISTEN_ADDR"),
    ],
)
def test_invalid_values_are_rejected(tmp_path, env, message):
    with pytest.raises(ConfigError, match=message):
        build_config(tmp_path, **env)


def test_parse_listen_addr():
    assert parse_listen_addr("127.0.0.1:9103") == ("127.0.0.1", 9103)
    with pytest.raises(ConfigError):
        parse_listen_addr(":8080")


def test_profile_is_stable_and_matches_parameters(tmp_path):
    config = build_config(tmp_path)
    assert config.profile() == {
        "id": "default",
        "sim_th": 0.4,
        "depth": 4,
        "max_children": 100,
        "mask_rules": [],
        "parametrize_numeric": True,
        "snapshot_compress": True,
    }
    other = build_config(tmp_path, SIM_TH="0.6")
    assert other.profile() != config.profile()


def test_from_env_defaults_like_an_empty_environment():
    # 默认值必须能在开发机上直接跑起来
    config = Config.from_env({})
    assert config.listen_addr == "0.0.0.0:8080"
    assert str(config.state_dir) == ".state"
    assert config.profile()["mask_rules"] == []
