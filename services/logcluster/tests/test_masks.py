"""脱敏规则。"""

from __future__ import annotations

import pytest
from drain3 import TemplateMiner
from src.config import Config
from src.engine import build_drain_config
from src.masks import available_rules, resolve_rules

from .conftest import build_config


def _miner(config: Config) -> TemplateMiner:
    """只要 masking + 数值参数化这两条路径，不涉及状态文件。"""
    return TemplateMiner(config=build_drain_config(config))


def test_available_rules_are_named():
    assert "ip" in available_rules()
    assert "uuid" in available_rules()


def test_unknown_rule_is_rejected():
    with pytest.raises(ValueError, match="unknown mask rule"):
        resolve_rules(["nope"])


def test_numeric_parametrization_alone_merges_high_cardinality_fields(tmp_path):
    """默认不需要脱敏规则：IP、百分比、设备名都能合并。"""
    miner = _miner(build_config(tmp_path))
    lines = [
        "connected to 10.0.0.1",
        "connected to 192.168.0.1",
        "disk usage 95% on /dev/sda1",
        "disk usage 12% on /dev/sdb2",
    ]
    for line in lines:
        miner.add_log_message(line)

    assert sorted(cluster.get_template() for cluster in miner.drain.clusters) == [
        "connected to <*>",
        "disk usage <*> on <*>",
    ]
    assert [cluster.size for cluster in miner.drain.clusters] == [2, 2]


def test_mask_rules_make_explicit_substitutions(tmp_path):
    """关掉数值参数化、改用显式规则时，替换记号出现在模板里。"""
    miner = _miner(
        build_config(tmp_path, MASK_RULES="ip,uuid", PARAMETRIZE_NUMERIC="false")
    )
    result = miner.add_log_message(
        "connected to 10.0.0.1 from 3f9a1c2e-1b4d-4e6f-8a9b-0c1d2e3f4a5b"
    )
    assert result["template_mined"] == "connected to <IP> from <UUID>"


def test_masking_is_applied_to_match_as_well(tmp_path):
    """match 也必须走同一套脱敏，否则学到的模板永远匹配不上。"""
    miner = _miner(build_config(tmp_path, MASK_RULES="ip", PARAMETRIZE_NUMERIC="false"))
    miner.add_log_message("connected to 10.0.0.1")
    assert miner.match("connected to 10.0.0.7") is not None
