"""测试共用脚手架。

配置一律通过 `Config.from_env` 构造（传 dict 而不是改进程环境），
这样配置解析本身也顺带被测到，而测试之间不会互相污染环境变量。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.api import create_app
from src.config import Config
from src.engine import DrainEngine
from src.metrics import Metrics

FIXTURES = SERVICE_ROOT / "tests" / "fixtures"

# drain3 在每次快照时都会打 INFO，测试里没必要看
logging.getLogger("drain3").setLevel(logging.WARNING)


def build_config(tmp_path: Path, **env: object) -> Config:
    base: dict[str, str] = {
        "STATE_DIR": str(tmp_path / "state"),
        "SIM_TH": "0.4",
        "DEPTH": "4",
    }
    base.update({key: str(value) for key, value in env.items()})
    return Config.from_env(base)


# profile 字段名 -> 环境变量名。契约测试用它把基线里的参数还原成配置，
# 保证"测试跑的参数"与"生成基线时的参数"不可能不一致。
PROFILE_ENV = {
    "sim_th": "SIM_TH",
    "depth": "DEPTH",
    "max_children": "MAX_CHILDREN",
    "parametrize_numeric": "PARAMETRIZE_NUMERIC",
    "snapshot_compress": "SNAPSHOT_COMPRESS",
}


def config_from_profile(profile: dict, tmp_path: Path) -> Config:
    env: dict[str, str] = {"STATE_DIR": str(tmp_path / "state")}
    for key, env_key in PROFILE_ENV.items():
        env[env_key] = str(profile[key])
    env["MASK_RULES"] = ",".join(profile["mask_rules"])
    return Config.from_env(env)


def build_engine(config: Config) -> tuple[DrainEngine, Metrics]:
    metrics = Metrics(version="test", commit="test")
    return DrainEngine(config, metrics), metrics


def build_client(config: Config) -> TestClient:
    engine, metrics = build_engine(config)
    return TestClient(create_app(config, engine, metrics))


def build_client_with_engine(config: Config) -> tuple[TestClient, DrainEngine]:
    """需要直接操作引擎（例如占住并发槽位）的测试用这个。"""
    engine, metrics = build_engine(config)
    return TestClient(create_app(config, engine, metrics)), engine


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return build_config(tmp_path)


@pytest.fixture
def engine(config: Config) -> DrainEngine:
    instance, _ = build_engine(config)
    return instance


@pytest.fixture
def client(config: Config) -> TestClient:
    return build_client(config)


def read_case(name: str) -> list[str]:
    """读一个契约样本，忽略空行与以 # 开头的注释行。"""
    text = (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
