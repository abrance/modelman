"""版本与构建溯源。

与日志聚类同源：Python 没有构建期，按优先级读 `build_info.json`（镜像构建时
由 Dockerfile 写入）、环境变量、本地 `git rev-parse`，都没有就写 `unknown`。

`VERSION` 是发布版本的唯一来源：`release-jev.yml` 会校验 `jev/vX.Y.Z` 这个 tag
与它一致，不一致直接失败。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0"

UNKNOWN = "unknown"

# src/ 的上一级：仓库里是 services/jev/，镜像里是 /app
_BUILD_INFO_PATH = Path(__file__).resolve().parent.parent / "build_info.json"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    git_commit: str
    build_time: str


def load() -> BuildInfo:
    payload = _from_file() or {}
    return BuildInfo(
        version=VERSION,
        git_commit=(
            payload.get("git_commit")
            or _from_env("GIT_COMMIT")
            or _git_rev_parse()
            or UNKNOWN
        ),
        build_time=payload.get("build_time") or _from_env("BUILD_TIME") or UNKNOWN,
    )


def _from_file() -> dict:
    try:
        raw = _BUILD_INFO_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _from_env(key: str) -> str | None:
    value = os.environ.get(key, "").strip()
    return value or None


def _git_rev_parse() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=_BUILD_INFO_PATH.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None
