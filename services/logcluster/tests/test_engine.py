"""引擎：聚类、匹配、上界、状态往返。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.engine import Overloaded, StateUnavailable
from src.state import STATE_SCHEMA_VERSION

from .conftest import build_config, build_engine

LINES = [
    "user alice logged in from 10.0.0.1",
    "user bob logged in from 10.0.0.2",
    "disk usage 95% on /dev/sda1",
]


def test_cluster_returns_one_result_per_line(tmp_path):
    engine, _ = build_engine(build_config(tmp_path))
    outcome = engine.cluster(LINES)

    assert [item["line"] for item in outcome.results] == LINES
    assert [item["change_type"] for item in outcome.results] == [
        "cluster_created",
        "cluster_template_changed",
        "cluster_created",
    ]
    assert outcome.cluster_count == 2
    # 第一行新建了簇，模板就是它自己，因此没有可变参数；
    # 第二行合并进去之后才出现 <*>
    assert outcome.results[0]["template"] == LINES[0]
    assert outcome.results[0]["parameters"] == []
    assert outcome.results[1]["template"] == "user <*> logged in from <*>"
    assert outcome.results[1]["parameters"] == ["bob", "10.0.0.2"]
    assert outcome.elapsed_ms >= 0.0


def test_cluster_is_deterministic(tmp_path):
    first, _ = build_engine(build_config(tmp_path / "a"))
    second, _ = build_engine(build_config(tmp_path / "b"))
    assert first.cluster(LINES).results == second.cluster(LINES).results


def test_match_does_not_learn(tmp_path):
    engine, _ = build_engine(build_config(tmp_path))
    engine.cluster(LINES)

    outcome = engine.match(
        ["user carol logged in from 10.0.0.9", "totally unrelated line"]
    )
    assert outcome.results[0]["matched"] is True
    assert outcome.results[0]["template"] == "user <*> logged in from <*>"
    assert outcome.results[1]["matched"] is False
    assert outcome.results[1]["cluster_id"] is None

    # 匹配不应新建模板
    assert engine.counts() == (2, 3)


def test_list_clusters_is_sorted_and_counted(tmp_path):
    engine, _ = build_engine(build_config(tmp_path))
    engine.cluster(LINES)

    clusters, total = engine.list_clusters()
    assert [item["cluster_id"] for item in clusters] == sorted(
        item["cluster_id"] for item in clusters
    )
    assert total == 3
    assert sum(item["size"] for item in clusters) == 3


def test_state_survives_a_restart(tmp_path):
    config = build_config(tmp_path)
    first, _ = build_engine(config)
    first.cluster(LINES)
    assert first.save_state("test") is True
    assert first.state_loaded is False  # 这次是从空树开始的

    second, _ = build_engine(config)
    assert second.state_loaded is True
    assert second.counts() == (2, 3)

    # 同样输入不再产生新模板
    replay = second.cluster(LINES)
    assert {item["change_type"] for item in replay.results} == {"none"}


def test_concurrency_is_bounded(tmp_path):
    config = build_config(tmp_path, MAX_CONCURRENCY="1", QUEUE_TIMEOUT_SECS="0.05")
    engine, _ = build_engine(config)

    # 直接占住唯一的槽位：这是对内部信号量的测试，比造两个真线程更确定
    engine._slots.acquire()
    try:
        with pytest.raises(Overloaded, match="服务忙"):
            engine.cluster(["user alice logged in"])
    finally:
        engine._slots.release()


def test_unavailable_state_degrades_instead_of_dying(tmp_path):
    config = build_config(tmp_path)
    path = Path(config.state_dir) / "drain_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    broken = json.dumps(
        {"schema_version": STATE_SCHEMA_VERSION + 1, "profile": {}, "drain": ""}
    )
    path.write_text(broken)

    engine, _ = build_engine(config)

    assert engine.load_error is not None
    assert "schema_version" in engine.load_error
    assert engine.is_ready() is False
    assert engine.state_loaded is False

    with pytest.raises(StateUnavailable):
        engine.cluster(["user alice logged in"])
    with pytest.raises(StateUnavailable):
        engine.match(["user alice logged in"])
    with pytest.raises(StateUnavailable):
        engine.list_clusters()

    # 关键：坏状态不会被覆盖
    assert engine.save_state("test") is False
    assert path.read_text() == broken


def test_state_is_persisted_without_an_explicit_save(tmp_path):
    """模板发生变化时 drain3 自己会落盘，重启后不需要人工保存。"""
    config = build_config(tmp_path)
    first, _ = build_engine(config)
    first.cluster(LINES)

    path = Path(config.state_dir) / "drain_state.json"
    assert path.exists(), "模板变化后应当已经写入状态文件"

    second, _ = build_engine(config)
    assert second.state_loaded is True
    assert second.counts() == first.counts()
