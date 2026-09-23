"""契约测试：模型行为有没有退化。

编译通过、接口返回 200 都不代表聚类结果没变。这里把所有样本按固定顺序
喂给一棵从空开始的状态树，逐行比对模板与簇 ID，再比对最终模板清单、
重放不变量、只读匹配探针与延迟预算。

基线变更等于改验收标准：只能由 `make fixtures SERVICE=logcluster` 重新生成，
并且必须人工 review 差异。
"""

from __future__ import annotations

import dataclasses
import json
import time
from importlib import metadata

import pytest
from src.config import Config
from src.engine import DrainEngine

from .conftest import FIXTURES, build_engine, config_from_profile, read_case

BASELINE = json.loads((FIXTURES / "baseline.json").read_text(encoding="utf-8"))


def test_baseline_covers_every_case_file():
    on_disk = sorted(path.stem for path in FIXTURES.glob("case_*.txt"))
    assert BASELINE["cases"] == on_disk


def test_per_case_expected_files_are_present():
    for name in BASELINE["cases"]:
        expected = json.loads(
            (FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8")
        )
        assert expected["name"] == name
        assert expected["line_count"] == len(read_case(name))


def test_baseline_profile_matches_service_defaults():
    """基线用的聚类参数必须就是服务默认参数，否则基线测的不是线上行为。"""
    assert BASELINE["profile"] == Config.from_env({}).profile()


def test_drain3_version_is_the_baselined_one():
    """换了 drain3 版本就必须重跑基线，这里拦下来。"""
    assert metadata.version("drain3") == BASELINE["drain3_version"]


@dataclasses.dataclass
class BaselineRun:
    """一次完整跑的结果。先把快照与逐样本结果取完，重放另起引擎，
    这样断言结果不依赖测试执行顺序。"""

    config: Config
    engine: DrainEngine
    results: list[tuple[str, list[str], object, int, int]]
    elapsed_ms: float


@pytest.fixture(scope="module")
def baseline_run(tmp_path_factory) -> BaselineRun:
    """按基线顺序跑一遍全部样本。"""
    tmp_path = tmp_path_factory.mktemp("contract")
    config = config_from_profile(BASELINE["profile"], tmp_path)
    engine, _ = build_engine(config)

    results = []
    started = time.perf_counter()
    for name in BASELINE["cases"]:
        lines = read_case(name)
        outcome = engine.cluster(lines)
        clusters, total = engine.counts()
        results.append((name, lines, outcome, clusters, total))
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    return BaselineRun(
        config=config, engine=engine, results=results, elapsed_ms=elapsed_ms
    )


def test_each_case_matches_its_baseline(baseline_run):
    assert [name for name, _, _, _, _ in baseline_run.results] == BASELINE["cases"]

    for name, lines, outcome, clusters, total in baseline_run.results:
        expected = json.loads(
            (FIXTURES / f"{name}.expected.json").read_text(encoding="utf-8")
        )
        got_templates = [item["template"] for item in outcome.results]
        got_ids = [item["cluster_id"] for item in outcome.results]
        got_changes = [item["change_type"] for item in outcome.results]

        assert len(outcome.results) == len(lines)
        assert got_templates == expected["templates"], f"{name} 模板变了"
        assert got_ids == expected["cluster_ids"], f"{name} 簇 ID 变了"
        assert got_changes == expected["change_types"], f"{name} change_type 变了"
        assert clusters == expected["cluster_count_after"], f"{name} 模板数变了"
        assert total == expected["total_size_after"], f"{name} 累计行数变了"


def test_final_state_matches_baseline(baseline_run):
    listing, total = baseline_run.engine.list_clusters()
    assert listing == BASELINE["templates"]
    assert total == BASELINE["final_total_size"]
    assert len(listing) == BASELINE["final_cluster_count"]


def test_replay_on_a_restarted_engine_creates_no_new_clusters(baseline_run):
    """重启后把同样的输入再喂一遍：状态能载回来，且不产生新模板。"""
    restarted, _ = build_engine(baseline_run.config)
    assert restarted.state_loaded is True

    created = 0
    for name in BASELINE["cases"]:
        for line in read_case(name):
            if restarted.cluster([line]).results[0]["change_type"] == "cluster_created":
                created += 1

    assert created == BASELINE["replay_new_clusters"]
    assert created == 0


def test_match_probes(baseline_run):
    """留出样本的只读匹配结果必须与基线一致。"""
    outcome = baseline_run.engine.match(
        [probe["line"] for probe in BASELINE["match_probes"]]
    )
    for got, expected in zip(outcome.results, BASELINE["match_probes"]):
        assert got["matched"] is expected["matched"], expected["line"]
        assert got["cluster_id"] == expected["cluster_id"], expected["line"]
        assert got["template"] == expected["template"], expected["line"]


def test_latency_stays_within_budget(baseline_run):
    """捕捉数量级退化，不捕捉抖动：预算在基线里是实测的 10 倍。"""
    budget_ms = BASELINE["budget"]["total_ms"]
    assert baseline_run.elapsed_ms <= budget_ms, (
        f"全部样本耗时 {baseline_run.elapsed_ms:.1f} ms 超出预算 {budget_ms} ms"
    )
