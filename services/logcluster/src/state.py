"""状态文件：格式封装、兼容性校验与原子写。

Drain3 自带的 `FilePersistence` 直接把字节 `write_bytes` 到目标路径，两个问题：

1. 写一半被打断就留下一个损坏的状态文件，而状态是这个服务唯一的累积资产；
2. 文件里只有 drain 内部结构，看不到它是用什么参数学出来的，
   换了 `SIM_TH` 或脱敏规则之后旧状态会被照单全收，输出静默变化。

所以这里自己实现一个 `PersistenceHandler`：外层套一个 JSON 信封，
写入走"临时文件 + rename"，读取时校验 schema 与 profile。

信封格式：

```json
{
  "schema_version": 1,
  "profile": {"id": "default", "sim_th": 0.4, ...},
  "saved_at": "2026-09-23T01:02:03Z",
  "drain": "<base64 of drain3 自己序列化出来的字节>"
}
```
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from drain3.persistence_handler import PersistenceHandler

# 信封格式的版本。改字段名或语义时才 +1；聚类参数变化由 profile 校验负责。
STATE_SCHEMA_VERSION = 1

STATE_FILE_NAME = "drain_state.json"


class StateError(Exception):
    """状态不可用。启动阶段视为致命：宁可拒绝启动也不要静默重建。"""


# 缺的键显示成 <missing>，其余值用 repr，便于区分 "0" 与 "<missing>"
_MISSING = object()


def profile_diff(saved: Mapping, current: Mapping) -> list[str]:
    """返回两版 profile 的差异，格式为 `key: saved -> current`。

    只报告键集合的并集，缺的键显示成 `<missing>`，便于运维直接看出
    是哪一项参数变了导致旧状态不可用。
    """
    keys = sorted(set(saved) | set(current))
    diffs = []
    for key in keys:
        before = saved.get(key, _MISSING)
        after = current.get(key, _MISSING)
        if before != after:
            diffs.append(f"{key}: {_render(before)} -> {_render(after)}")
    return diffs


def _render(value: object) -> str:
    return "<missing>" if value is _MISSING else repr(value)


def ensure_writable(state_dir: Path) -> None:
    """状态目录必须可写。

    写不进去的服务每次重启都要从头学模板，与其到第一次快照才发现，
    不如启动时就报错。这是配置/部署问题，不是数据问题，所以直接退 2。
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix=".writable-"):
            pass
    except OSError as exc:
        raise StateError(f"状态目录不可写 {state_dir}: {exc}") from exc


class AtomicStatePersistence(PersistenceHandler):
    """带信封与原子写的 drain3 持久化后端。"""

    def __init__(self, path: Path, profile: Mapping) -> None:
        self.path = path
        self.profile = dict(profile)
        self.loaded = False
        self.saved_at: str | None = None
        self.save_count = 0

    # ── 读取 ────────────────────────────────────────────────────────────

    def load_state(self) -> bytes | None:
        """返回 drain3 需要的原始字节；没有状态就返回 None。"""
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise StateError(f"读取状态文件失败 {self.path}: {exc}") from exc

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateError(
                f"状态文件不是合法 JSON（{self.path}）：{exc}。"
                "确认不需要该状态后删除它再启动。"
            ) from exc
        if not isinstance(envelope, dict):
            raise StateError(f"状态文件顶层不是对象（{self.path}）")

        version = envelope.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise StateError(
                f"状态文件 schema_version={version!r} 与本镜像的 "
                f"{STATE_SCHEMA_VERSION} 不一致（{self.path}）。"
                "旧状态不会被自动覆盖或升级。"
            )

        saved_profile = envelope.get("profile")
        if not isinstance(saved_profile, dict):
            raise StateError(f"状态文件缺少 profile（{self.path}）")
        diffs = profile_diff(saved_profile, self.profile)
        if diffs:
            raise StateError(
                f"状态文件与当前聚类参数不兼容（{self.path}）："
                + "；".join(diffs)
                + "。改参数后继续用旧模板会让输出静默变化，因此拒绝启动。"
            )

        payload = envelope.get("drain")
        if not isinstance(payload, str):
            raise StateError(f"状态文件缺少 drain 字段（{self.path}）")
        try:
            state = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise StateError(
                f"状态文件 drain 字段不是合法 base64（{self.path}）"
            ) from exc

        self.saved_at = envelope.get("saved_at")
        self.loaded = True
        return state

    # ── 写入 ────────────────────────────────────────────────────────────

    def save_state(self, state: bytes) -> None:
        envelope = {
            "schema_version": STATE_SCHEMA_VERSION,
            "profile": self.profile,
            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "drain": base64.b64encode(state).decode("ascii"),
        }
        blob = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # 同一个目录里的临时文件 + rename：只有 rename 是可见的，
            # 中途失败最多留下一个 .tmp，不会破坏已有的状态文件。
            # 不做目录 fsync：rename 本身是原子的，掉电最坏情况是保留上一个
            # 状态文件，这正是想要的结果。
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=directory,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except OSError as exc:
            raise StateError(f"写入状态文件失败 {self.path}: {exc}") from exc

        self.saved_at = envelope["saved_at"]
        self.save_count += 1

    # ── 供 /models 报告 ─────────────────────────────────────────────────

    def metadata(self) -> dict:
        size = None
        try:
            size = self.path.stat().st_size
        except OSError:
            size = None
        return {
            "path": str(self.path),
            "schema_version": STATE_SCHEMA_VERSION,
            "profile": self.profile,
            "loaded": self.loaded,
            "saved_at": self.saved_at,
            "bytes": size,
        }
