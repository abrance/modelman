"""测试共用脚手架。

配置一律通过 `Config.from_env` 构造（传 dict 而不是改进程环境），
这样配置解析本身也顺带被测到，而测试之间不会互相污染环境变量。

需要真实权重的用例统一用 `@pytest.mark.weights`：本地没跑过 `make build` 时
用 `pytest -m "not weights"` 跳过，CI 里权重一定在（service.mk 的构建步骤会取回）。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVICE_ROOT.parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from src.api import create_app  # noqa: E402
from src.config import Config  # noqa: E402
from src.engine import DecisionEngine  # noqa: E402
from src.metrics import Metrics  # noqa: E402

FIXTURES = SERVICE_ROOT / "tests" / "fixtures"
MODELS_DIR = SERVICE_ROOT / "models"
DIGESTS_PATH = REPO_ROOT / "registry" / "jev-digests.json"

logging.getLogger("transformers").setLevel(logging.WARNING)

# 权重在不在，决定要不要跑带模型的用例
WEIGHTS_PRESENT = (MODELS_DIR / "multilingual" / "model.safetensors").is_file()
requires_weights = pytest.mark.skipif(
    not WEIGHTS_PRESENT,
    reason="本地没有权重，先跑 make build SERVICE=jev",
)


def build_config(tmp_path: Path, **env: object) -> Config:
    base: dict[str, str] = {
        "MODELS_DIR": str(tmp_path / "models"),
        "MODEL_TIER": "multilingual",
        "PRELOAD": "false",
    }
    base.update({key: str(value) for key, value in env.items()})
    return Config.from_env(base)


def loaded_config(tmp_path: Path, **env: object) -> Config:
    base: dict[str, str] = {"MODELS_DIR": str(MODELS_DIR), "PRELOAD": "true"}
    base.update({key: str(value) for key, value in env.items()})
    return Config.from_env(base)


def build_engine(config: Config) -> tuple[DecisionEngine, Metrics]:
    metrics = Metrics(version="test", commit="test")
    return DecisionEngine(config, metrics), metrics


def build_client(config: Config) -> TestClient:
    engine, metrics = build_engine(config)
    return TestClient(create_app(config, engine, metrics))


def build_client_loaded(tmp_path: Path, **env: object) -> tuple[TestClient, DecisionEngine]:
    config = loaded_config(tmp_path, **env)
    engine, metrics = build_engine(config)
    return TestClient(create_app(config, engine, metrics)), engine


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return build_config(tmp_path)


@pytest.fixture
def client(config: Config) -> TestClient:
    return build_client(config)


@pytest.fixture(scope="session")
def digest_payload() -> dict:
    return json.loads(DIGESTS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def cases() -> list[dict]:
    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="session")
def baseline() -> dict:
    return json.loads((FIXTURES / "baseline.json").read_text(encoding="utf-8"))
