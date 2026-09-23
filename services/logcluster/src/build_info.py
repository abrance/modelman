"""版本与构建溯源。

OCR 服务用 `build.rs` 把提交与构建时间编进二进制；Python 没有构建期，
所以这里按优先级读三个来源：

1. `build_info.json`，镜像构建时由 Dockerfile 写入（`GIT_COMMIT` / `BUILD_TIME`
   两个 build-arg）；
2. 环境变量 `GIT_COMMIT` / `BUILD_TIME`（CI 里不带 build-arg 时的兜底）；
3. 本地 `git rev-parse`，给 `make run` 用；
4. 都没有就写 `unknown`，不编一个假的构建时间。

`VERSION` 是发布版本的唯一来源：`release-logcluster.yml` 会校验
`logcluster/vX.Y.Z` 这个 tag 与它一致，不一致直接失败，避免出现
"镜像里报的版本和 tag 对不上"。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0"

UNKNOWN = "unknown"

# src/ 的上一级：仓库里是 services/logcluster/，镜像里是 /app
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
