"""状态文件：信封格式、兼容性校验、原子写。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from src.state import (
    STATE_SCHEMA_VERSION,
    AtomicStatePersistence,
    StateError,
    ensure_writable,
    profile_diff,
)

PROFILE = {
    "id": "default",
    "sim_th": 0.4,
    "depth": 4,
    "max_children": 100,
    "mask_rules": [],
    "parametrize_numeric": True,
    "snapshot_compress": True,
}


def _persistence(tmp_path: Path, profile: dict | None = None) -> AtomicStatePersistence:
    return AtomicStatePersistence(
        path=tmp_path / "drain_state.json", profile=profile or PROFILE
    )


def test_missing_state_is_not_an_error(tmp_path):
    persistence = _persistence(tmp_path)
    assert persistence.load_state() is None
    assert persistence.loaded is False


def test_roundtrip(tmp_path):
    persistence = _persistence(tmp_path)
    persistence.save_state(b"drain-bytes")

    assert persistence.loaded is False  # 只写不读
    assert persistence.load_state() == b"drain-bytes"
    assert persistence.loaded is True
    assert persistence.saved_at is not None

    reloaded = _persistence(tmp_path)
    assert reloaded.load_state() == b"drain-bytes"


def test_envelope_is_json_and_wraps_drain_bytes(tmp_path):
    persistence = _persistence(tmp_path)
    persistence.save_state(b"drain-bytes")
    envelope = json.loads((tmp_path / "drain_state.json").read_text())
    assert envelope["schema_version"] == STATE_SCHEMA_VERSION
    assert envelope["profile"] == PROFILE
    assert base64.b64decode(envelope["drain"]) == b"drain-bytes"


def test_save_leaves_no_temporary_files(tmp_path):
    persistence = _persistence(tmp_path)
    persistence.save_state(b"one")
    persistence.save_state(b"two")
    leftovers = [
        path.name for path in tmp_path.iterdir() if path.name != "drain_state.json"
    ]
    assert leftovers == []
    assert persistence.save_count == 2


def test_schema_version_mismatch_is_refused(tmp_path):
    path = tmp_path / "drain_state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": STATE_SCHEMA_VERSION + 1,
                "profile": PROFILE,
                "drain": "",
            }
        )
    )
    with pytest.raises(StateError, match="schema_version"):
        _persistence(tmp_path).load_state()


def test_profile_mismatch_is_refused_and_explained(tmp_path):
    persistence = _persistence(tmp_path)
    persistence.save_state(b"drain-bytes")

    changed = dict(PROFILE, sim_th=0.6)
    with pytest.raises(StateError) as excinfo:
        _persistence(tmp_path, changed).load_state()
    message = str(excinfo.value)
    assert "sim_th" in message
    assert "0.4" in message and "0.6" in message


def test_corrupt_state_is_refused(tmp_path):
    (tmp_path / "drain_state.json").write_text("{not json")
    with pytest.raises(StateError, match="不是合法 JSON"):
        _persistence(tmp_path).load_state()


def test_broken_envelope_fields_are_refused(tmp_path):
    path = tmp_path / "drain_state.json"

    path.write_text(json.dumps({"schema_version": STATE_SCHEMA_VERSION, "drain": ""}))
    with pytest.raises(StateError, match="profile"):
        _persistence(tmp_path).load_state()

    path.write_text(
        json.dumps(
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "profile": PROFILE,
                "drain": "!!!not base64",
            }
        )
    )
    with pytest.raises(StateError, match="base64"):
        _persistence(tmp_path).load_state()

    path.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(StateError, match="顶层不是对象"):
        _persistence(tmp_path).load_state()


def test_metadata_reports_what_operators_need(tmp_path):
    persistence = _persistence(tmp_path)
    persistence.save_state(b"drain-bytes")
    metadata = persistence.metadata()
    assert metadata["schema_version"] == STATE_SCHEMA_VERSION
    assert metadata["profile"] == PROFILE
    assert metadata["bytes"] > 0
    assert metadata["path"].endswith("drain_state.json")


def test_profile_diff_reports_missing_keys():
    diffs = profile_diff({"sim_th": 0.4}, {"sim_th": 0.4, "depth": 4})
    assert diffs == ["depth: <missing> -> 4"]
    assert profile_diff({"a": 1}, {"a": 1}) == []


def test_ensure_writable_creates_and_probes(tmp_path):
    target = tmp_path / "nested" / "state"
    ensure_writable(target)
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_ensure_writable_rejects_a_file_path(tmp_path):
    path = tmp_path / "not-a-dir"
    path.write_text("x")
    with pytest.raises(StateError, match="状态目录不可写"):
        ensure_writable(path)
