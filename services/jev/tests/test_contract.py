"""契约测试：模型行为回归的门禁。

三件事必须一起成立，缺一件基线就失去意义：

1. 权重摘要与 `registry/jev-digests.json` 一致——否则测的不是线上那套权重；
2. 逐样本判定结果与基线一致（选项 argmax 精确、概率在容差内）；
3. 中位延迟不出现数量级退化。

基线由 `make fixtures SERVICE=jev` 生成，提交前人工确认。
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

import pytest

from src.config import Config
from src.engine import DecisionEngine
from src.metrics import Metrics
from tests.conftest import DIGESTS_PATH, SERVICE_ROOT, requires_weights

pytestmark = requires_weights

REGISTRY = SERVICE_ROOT.parent.parent / "registry" / "jev.yaml"
REPEATS = 3


def _engine() -> DecisionEngine:
    config = Config.from_env({"MODELS_DIR": str(SERVICE_ROOT / "models"), "PRELOAD": "true"})
    engine = DecisionEngine(config, Metrics(version="contract", commit="contract"))
    assert engine.is_ready(), engine.load_error
    return engine


def _close(expected: float, actual: float, tolerance: float) -> bool:
    return abs(expected - actual) <= tolerance


def _compare(expected: dict, actual: dict, tolerance: float, path: str) -> list[str]:
    problems: list[str] = []
    for key, want in expected.items():
        if key not in actual:
            problems.append(f"{path}.{key}: 结果里缺少这个键")
            continue
        got = actual[key]
        if isinstance(want, dict):
            problems.extend(_compare(want, got, tolerance, f"{path}.{key}"))
        elif isinstance(want, (int, float)) and not isinstance(want, bool):
            if not _close(float(want), float(got), tolerance):
                problems.append(f"{path}.{key}: 期望 {want}，实际 {got}")
        elif want != got:
            problems.append(f"{path}.{key}: 期望 {want!r}，实际 {got!r}")
    return problems


def test_baseline_matches_registry_digests(baseline: dict, digest_payload: dict) -> None:
    model = baseline["model"]
    assert model["revision"] == digest_payload["revision"]
    assert model["files"] == digest_payload["files"]


def test_registry_file_agrees_with_digests(digest_payload: dict) -> None:
    """registry/jev.yaml 是"哪个模型在跑"的唯一事实来源，它与摘要文件必须一致。"""
    text = REGISTRY.read_text(encoding="utf-8")
    assert digest_payload["revision"] in text, "registry/jev.yaml 里的 revision 与摘要文件不一致"
    assert "digest_manifest: registry/jev-digests.json" in text
    tier = digest_payload["subfolder"]
    assert re.search(rf"- id: {tier}\b", text), f"registry/jev.yaml 里没有档位 {tier}"


def test_baseline_params_match_defaults(baseline: dict) -> None:
    """基线里的参数指纹必须等于服务默认值，否则测试跑的参数与基线不是一套。"""
    config = Config.from_env({"MODELS_DIR": str(SERVICE_ROOT / "models")})
    assert baseline["params"] == config.profile()


def test_latency_budget_is_declared(baseline: dict) -> None:
    budget = baseline["latency_budget_ms"]
    assert budget > 0
    text = REGISTRY.read_text(encoding="utf-8")
    assert f"latency_budget_ms: {budget}" in text


def test_answers_reproduce_baseline(baseline: dict, cases: list[dict]) -> None:
    engine = _engine()
    tolerance = baseline["tolerance"]
    by_id = {item["id"]: item for item in baseline["cases"]}
    assert {case["id"] for case in cases} == set(by_id), "样本集合与基线不一致"

    problems: list[str] = []
    for case in cases:
        expected = by_id[case["id"]]
        outcome = engine.decide(case["state"], case["questions"])
        problems.extend(
            _compare(expected["answers"], outcome.answers, tolerance, case["id"])
        )
    assert not problems, "判定结果与基线不一致：\n" + "\n".join(problems)


def test_median_latency_stays_within_budget(baseline: dict, cases: list[dict]) -> None:
    engine = _engine()
    budget = baseline["latency_budget_ms"]
    medians = []
    for case in cases:
        samples = []
        for _ in range(REPEATS):
            samples.append(engine.decide(case["state"], case["questions"]).elapsed_ms)
        medians.append(statistics.median(samples))

    overall = statistics.median(medians)
    # 只拦数量级退化：换了线程数、权重没常驻、被限流都会撞在这里
    assert overall <= budget, (
        f"中位延迟 {overall:.0f} ms 超过预算 {budget} ms"
        f"（基线记录 {baseline['median_latency_ms']} ms）"
    )


def test_answers_have_distribution_for_choice(baseline: dict) -> None:
    """choice 答案必须同时带 probabilities 与 distribution，前端与 pi-jev 各自读一个。"""
    for item in baseline["cases"]:
        for qid, answer in item["answers"].items():
            if answer.get("type") == "choice":
                assert isinstance(answer.get("distribution"), dict), (item["id"], qid)
                assert set(answer["distribution"]) == set(answer["probabilities"])
                total = sum(answer["distribution"].values())
                assert total == pytest.approx(1.0, abs=0.05), (item["id"], qid, total)
