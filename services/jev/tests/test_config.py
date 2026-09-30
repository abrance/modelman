"""配置解析的测试。默认值、边界与错误信息都要能被断言到。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config, ConfigError, parse_listen_addr
from tests.conftest import build_config


def test_defaults_are_usable_on_a_dev_machine(tmp_path: Path) -> None:
    config = Config.from_env({"MODELS_DIR": str(tmp_path)})
    assert config.listen_addr == "0.0.0.0:8080"
    assert config.model_tier == "multilingual"
    assert config.device == "cpu"
    assert config.max_concurrency == 2
    assert config.auth_token is None
    assert config.preload is True
    assert config.max_len == 1024


def test_model_dir_is_tier_scoped(tmp_path: Path) -> None:
    config = Config.from_env({"MODELS_DIR": str(tmp_path)})
    assert config.model_dir == tmp_path / "multilingual"


def test_unknown_tier_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MODEL_TIER"):
        Config.from_env({"MODELS_DIR": str(tmp_path), "MODEL_TIER": "big"})


def test_head_budget_cannot_exceed_state_budget(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="HEAD_MAX_LEN"):
        build_config(tmp_path, MAX_LEN=128, HEAD_MAX_LEN=256)


def test_max_len_range_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MAX_LEN"):
        build_config(tmp_path, MAX_LEN=99999)


def test_bool_parsing_rejects_garbage(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PRELOAD"):
        build_config(tmp_path, PRELOAD="maybe")


def test_auth_token_empty_means_disabled(tmp_path: Path) -> None:
    assert build_config(tmp_path, AUTH_TOKEN="  ").auth_token is None
    assert build_config(tmp_path, AUTH_TOKEN="secret").auth_token == "secret"


def test_listen_addr_validation() -> None:
    assert parse_listen_addr("0.0.0.0:8080") == ("0.0.0.0", 8080)
    for bad in ("8080", "0.0.0.0:", "0.0.0.0:abc", "0.0.0.0:70000"):
        with pytest.raises(ConfigError):
            parse_listen_addr(bad)


def test_profile_is_stable(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    assert config.profile() == {
        "tier": "multilingual",
        "device": "cpu",
        "max_len": 1024,
        "head_max_len": 256,
    }
