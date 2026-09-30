"""引擎层测试：请求校验、加载失败降级、摘要清单读取。

不需要真实权重的部分占多数；需要推理的用例标了 `weights`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Config
from src.engine import (
    DecisionEngine,
    InvalidRequest,
    ModelUnavailable,
    validate_request,
)
from src.metrics import Metrics
from tests.conftest import build_config, requires_weights

CHOICE = {
    "route": {
        "type": "choice",
        "instructions": "pick one",
        "criteria": {"a": "first", "b": "second"},
    }
}


def test_validate_accepts_text_and_object_state(tmp_path: Path) -> None:
    config = build_config(tmp_path)
    validate_request("some text", CHOICE, config)
    validate_request({"prompt": "some text"}, CHOICE, config)


@pytest.mark.parametrize(
    "state, questions, match",
    [
        ("", CHOICE, "state 不能为空"),
        ("   ", CHOICE, "state 不能为空"),
        (None, CHOICE, "state 不能为空"),
        ("x", {}, "questions 不能为空"),
        ("x", {"q": {"type": "bogus", "instructions": "?"}}, "不是"),
        ("x", {"q": {"type": "choice", "instructions": "?"}}, "缺少 criteria"),
        (
            "x",
            {"q": {"type": "choice", "instructions": "?", "criteria": {"a": "only"}}},
            "至少需要 2 个选项",
        ),
        ("x", {"q": {"type": "noul"}}, "缺少 instructions"),
        ("x", {"q": "not-a-dict"}, "必须是对象"),
    ],
)
def test_validate_rejects_bad_requests(tmp_path: Path, state, questions, match) -> None:
    config = build_config(tmp_path)
    with pytest.raises(InvalidRequest, match=match):
        validate_request(state, questions, config)


def test_state_length_is_bounded(tmp_path: Path) -> None:
    config = build_config(tmp_path, MAX_STATE_CHARS=10)
    with pytest.raises(InvalidRequest, match="MAX_STATE_CHARS"):
        validate_request("x" * 11, CHOICE, config)


def test_question_count_is_bounded(tmp_path: Path) -> None:
    config = build_config(tmp_path, MAX_QUESTIONS=1)
    questions = {f"q{index}": CHOICE["route"] for index in range(2)}
    with pytest.raises(InvalidRequest, match="MAX_QUESTIONS"):
        validate_request("x", questions, config)


def test_option_count_is_bounded(tmp_path: Path) -> None:
    config = build_config(tmp_path, MAX_OPTIONS=3)
    criteria = {f"option_{index}": "text" for index in range(4)}
    questions = {
        "q": {"type": "choice", "instructions": "?", "criteria": criteria}
    }
    with pytest.raises(InvalidRequest, match="选项数"):
        validate_request("x", questions, config)


def test_engine_reports_load_error_without_failing(tmp_path: Path) -> None:
    """权重目录不存在时引擎照常构造，只是未就绪——进程要活着才能用 HTTP 看原因。"""
    config = build_config(tmp_path, PRELOAD="true")
    metrics = Metrics(version="test", commit="test")
    engine = DecisionEngine(config, metrics)
    assert engine.is_ready() is False
    assert engine.load_error is not None
    with pytest.raises(ModelUnavailable):
        engine.decide("x", CHOICE)
    rental = engine.model_metadata()
    assert rental["tier"] == "multilingual"
    assert rental["digest_verified"] is False


def test_digest_manifest_is_read(tmp_path: Path) -> None:
    model_dir = tmp_path / "models" / "multilingual"
    model_dir.mkdir(parents=True)
    (model_dir / "sha256.json").write_text(
        json.dumps({"model.safetensors": "deadbeef", "revision": "abc123"}),
        encoding="utf-8",
    )
    config = build_config(tmp_path, PRELOAD="false")
    engine = DecisionEngine(config, Metrics(version="t", commit="t"))
    metadata = engine.model_metadata()
    assert metadata["digests"] == 2  # 含 revision 这一项元数据
    assert metadata["revision"] == "abc123"


def test_broken_manifest_is_reported(tmp_path: Path) -> None:
    model_dir = tmp_path / "models" / "multilingual"
    model_dir.mkdir(parents=True)
    (model_dir / "sha256.json").write_text("{not json", encoding="utf-8")
    config = build_config(tmp_path, PRELOAD="false")
    engine = DecisionEngine(config, Metrics(version="t", commit="t"))
    assert engine.load_error is not None
    assert "不可读" in engine.load_error


@requires_weights
def test_engine_decides_and_normalizes_distribution(tmp_path: Path) -> None:
    from tests.conftest import build_engine, loaded_config

    config: Config = loaded_config(tmp_path)
    engine, _ = build_engine(config)
    assert engine.is_ready(), engine.load_error

    outcome = engine.decide({"prompt": "查一下 k8s 里 pod 为什么一直 pending"}, CHOICE)
    answer = outcome.answers["route"]
    # 上游把选项概率放在 probabilities，Jev 客户端读 distribution，两个都要在
    assert "probabilities" in answer
    assert answer["distribution"] == answer["probabilities"]
    assert answer["choice"] in CHOICE["route"]["criteria"]
    assert outcome.elapsed_ms > 0
    assert outcome.usage["input_tokens"] > 0


@requires_weights
def test_engine_verifies_digests(tmp_path: Path) -> None:
    from tests.conftest import build_engine, loaded_config

    config = loaded_config(tmp_path)
    engine, _ = build_engine(config)
    metadata = engine.model_metadata()
    assert metadata["digest_verified"] is True
    assert metadata["weights_bytes"] > 600_000_000
