#!/usr/bin/env python3
"""把 Laya 权重取回本地并按摘要校验，供构建期与开发机的 `make build` 使用。

为什么不是"权重进仓库"：这是一个公开仓库，单文件上限 100 MB，而权重是 644 MB，
LFS 的额度也扛不住每次 CI 拉取。因此这里把"可复现"落在不可变 revision 加逐文件
sha256 上：`registry/jev-digests.json` 是摘要的唯一事实来源，下载后逐个校验，
不符就失败——不需要人工比对，也不需要信任传输路径。

用法：

    python tools/fetch_weights.py --tier multilingual --digests registry/jev-digests.json --out models

已存在且摘要一致时不重复下载。国内网络直连 huggingface.co 常常不通，
用 `HF_ENDPOINT=https://hf-mirror.com` 换源。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_digests(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise SystemExit(f"{path} 里没有 files 映射")
    return payload


def verify(model_dir: Path, files: dict) -> list[str]:
    """返回摘要不符的文件列表。缺失也算不符。"""
    problems = []
    for name, expected in files.items():
        target = model_dir / name
        if not target.is_file():
            problems.append(f"{name}: 缺失")
            continue
        got = sha256_file(target)
        if got.lower() != str(expected).lower():
            problems.append(f"{name}: 期望 {expected}，实际 {got}")
    return problems


def download(payload: dict, model_dir: Path, endpoint: str | None) -> None:
    if endpoint:
        # transformers / huggingface_hub 都读这个变量
        os.environ["HF_ENDPOINT"] = endpoint
    from huggingface_hub import snapshot_download

    repo = payload["repo"]
    subfolder = payload.get("subfolder") or ""
    prefix = f"{subfolder}/" if subfolder else ""
    names = list(payload["files"])
    snapshot = snapshot_download(
        repo,
        revision=payload["revision"],
        allow_patterns=[prefix + name for name in names],
    )
    source = Path(snapshot) / subfolder if subfolder else Path(snapshot)
    for name in names:
        src = source / name
        if not src.is_file():
            raise SystemExit(f"上游快照里没有 {name}")
        dst = model_dir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 解引用符号链接：镜像构建上下文与容器内都不需要 hub 的缓存布局
        shutil.copyfile(src, dst)


def write_manifest(payload: dict, model_dir: Path, files: dict) -> None:
    manifest = dict(files)
    manifest["revision"] = payload["revision"]
    (model_dir / "sha256.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="取回并校验 Laya 权重")
    parser.add_argument("--tier", default="multilingual", help="档位，即 models/ 下的子目录名")
    parser.add_argument("--digests", default="registry/jev-digests.json")
    parser.add_argument("--out", default="models", help="权重的落地目录")
    parser.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", ""))
    args = parser.parse_args()

    digests_path = Path(args.digests)
    payload = load_digests(digests_path)
    model_dir = Path(args.out) / args.tier
    model_dir.mkdir(parents=True, exist_ok=True)

    problems = verify(model_dir, payload["files"])
    if problems:
        print(f"权重缺失或摘要不符（{len(problems)} 项），开始下载：{payload['repo']}", flush=True)
        for item in problems:
            print(f"  - {item}", flush=True)
        download(payload, model_dir, args.endpoint or None)
        problems = verify(model_dir, payload["files"])
        if problems:
            print("下载后摘要仍不符，拒绝交付：", file=sys.stderr)
            for item in problems:
                print(f"  - {item}", file=sys.stderr)
            return 1
    else:
        print(f"权重已就绪且摘要一致：{model_dir}", flush=True)

    write_manifest(payload, model_dir, payload["files"])
    total = sum((model_dir / name).stat().st_size for name in payload["files"])
    print(
        f"ok: {model_dir} 共 {len(payload['files'])} 个文件，"
        f"{total / 1e6:.1f} MB，revision {payload['revision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
