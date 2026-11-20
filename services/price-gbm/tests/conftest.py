"""测试共用脚手架。

配置一律通过 `Config.from_env` 构造（传 dict 而不是改进程环境），
配置解析本身顺带被测到，测试之间不污染环境变量。
"""

from __future__ import annotations

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
from src.engine import PriceEngine  # noqa: E402
from src.metrics import Metrics  # noqa: E402

FIXTURES = SERVICE_ROOT / "tests" / "fixtures"
MODELS_DIR = SERVICE_ROOT / "models"

# 权重（booster 文本）随仓库交付，正常一定在；保留跳过口径应对干净检出
MODELS_PRESENT = (MODELS_DIR / "gbm-hour-v1" / "q50.txt").is_file()
requires_models = pytest.mark.skipif(
    not MODELS_PRESENT, reason="本地没有权重（booster 文本随仓库交付，正常不该发生）"
)


def build_config(**env: object) -> Config:
    base: dict[str, str] = {"MODELS_DIR": str(MODELS_DIR)}
    base.update({key: str(value) for key, value in env.items()})
    return Config.from_env(base)


def build_client(config: Config) -> TestClient:
    metrics = Metrics(version="test", commit="test")
    engine = PriceEngine(config)
    app = create_app(config, engine, metrics)
    return TestClient(app)


def fixture_cases() -> list[dict]:
    import json

    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
