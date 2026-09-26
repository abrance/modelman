#!/usr/bin/env python3
"""文档一致性检查。

背景：这个仓库的文档已经烂过三次——鉴权口径在多个文件里互相矛盾、
页面路径改了旧说法没清、一个参数名（`LIMIT_CONCURRENCY`）被写错归属后
扩散到 5 处。根因都是"同一件事有多个副本"。这个脚本守住四条线：

1. md 相对链接与反引号里的仓库路径必须存在；
2. AGENTS.md 与 docs/design.md 的服务契约端点集合必须一致；
3. 已知的腐化词（过期口径、写错的归属、过期状态）出现即失败；
4. 文档里引用的服务名必须是真实存在的服务。

它是 `make docs-check` 的实现，CI 的「格式与静态检查」会跑。
纯标准库，不引依赖。新规则就往下面的常量表里加。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC_ROOTS = ["docs", "services", "README.md", "AGENTS.md"]

# ── 已知腐化词：出现即失败 ────────────────────────────────────────────
# 每条都对应一次真实发生过的不一致。左边是模式，右边是出问题时该看哪。
STALE_PATTERNS: list[tuple[str, str]] = [
    # 鉴权口径改过三轮（D15/D16/D17），旧说法曾经残留
    (r"对外暴露前必须", "旧鉴权口径，现为不启用就开放（design.md D15-D17）"),
    (
        r"必须先[^。\n]*(启用|设置|加)[^。\n]*`?AUTH_TOKEN",
        "旧鉴权口径，现为不启用就开放（design.md D15-D17）",
    ),
    (
        r"(启用|设置)[^。\n]*token[^。\n]*(再|才|先)开放",
        "旧鉴权口径，现为不启用就开放（design.md D15-D17）",
    ),
    (r"必须设置 `?AUTH_TOKEN`?", "旧鉴权口径，现为不启用就开放（design.md D15-D17）"),
    (r"尚未补齐", "logcluster 根路径已有页面，该说法过期"),
    (r"待发版", "状态词过期：发过版就该写已上线"),
    # 页面从 /ui 挪到根路径（ocr v0.1.3），旧路径残留
    (r"`/ui(/[a-z.]+)?`", "页面在根路径 /，/ui 已下线（ocr v0.1.3）"),
    # 历史解释句豁免：讲"为什么挪走"的段落允许提到旧路径
    (r"/ui/app\.(css|js)", "页面资源在 /app.css、/app.js（ocr v0.1.3）"),
    # 参数归属写错过（LIMIT_CONCURRENCY 是 logcluster 的，不是 ocr 的）
    (
        r"MAX_CONCURRENCY`?、`?LIMIT_CONCURRENCY",
        "LIMIT_CONCURRENCY 是日志聚类的参数，别和 OCR 并列",
    ),
    # 主机迁移（cloud2 compose → cloud3 k3s）后过期的说法
    (
        r"只绑(定)? `?127\.0\.0\.1`?",
        "cloud3 走 k8s，暴露面由 Service/IngressRoute 决定",
    ),
    (r"ptcdoc", "拼写错误，应为 ptdoc（见 cops 仓库）"),
    (r"docker compose pull", "部署已是 k8s（cloud3），compose 流程过期"),
]

# md 链接与反引号路径引用都要能解析
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# 只查带目录前缀的引用（docs/x.md、services/ocr/src/api.rs）；
# 单文件名（`smoke.sh`、`design.md`）是行文指代，不唯一指向仓库根，误报太多。
BACKTICK_PATH_RE = re.compile(
    r"`((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:md|ya?ml|toml|sh|py|rs|json|txt))`"
)
SERVICE_RE = re.compile(r"\bmodelman-([a-z][a-z0-9-]*)\b")

# md 链接里锚点先不做（当前一个都没用），外部 http 链接也不查
SKIP_PREFIXES = ("http://", "https://", "mailto:")


def doc_files() -> list[Path]:
    files: list[Path] = []
    for root in DOC_ROOTS:
        path = REPO / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                sorted(
                    p
                    for p in path.rglob("*.md")
                    if ".venv" not in p.parts and "target" not in p.parts
                )
            )
    return files


def existing_services() -> set[str]:
    return {
        p.name
        for p in (REPO / "services").iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }


def check_links(files: list[Path], problems: list[str]) -> None:
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1).strip()
            if target.startswith(SKIP_PREFIXES):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                problems.append(
                    f"{path.relative_to(REPO)}: 链接指向不存在的文件 {target}"
                )
        for match in BACKTICK_PATH_RE.finditer(text):
            target = match.group(1)
            # 反引号引用按「先仓库根、后文档所在目录」解析：
            # docs/ocr-ui.md 里的 `design.md` 指的是 docs/design.md，
            # `src/config.rs` 指的是 services/ocr/src/config.rs。
            candidates = [REPO / target, path.parent / target]
            if not any(c.exists() for c in candidates):
                problems.append(
                    f"{path.relative_to(REPO)}: 反引号里引用的路径不存在 `{target}`"
                )


def _contract_endpoints(text: str, section_title: str) -> set[str]:
    """抓一个文档里指定章节的契约端点集合。"""
    for chunk in re.split(r"^## ", text, flags=re.MULTILINE):
        if chunk.split("\n")[0].strip() != section_title:
            continue
        found = set(re.findall(r"`(?:GET|POST) (/[A-Za-z0-9_./-]*)`", chunk))
        # AGENTS 是行文列举（`GET /`、`/livez`、`/metrics`（需鉴权）…），
        # 不全带方法前缀；design 是表格（都带）。两种都收。
        found |= set(re.findall(r"`(/[A-Za-z0-9_./-]*)`", chunk))
        return found
    return set()


def check_contract(files: list[Path], problems: list[str]) -> None:
    """AGENTS.md 与 design.md 的契约端点集合必须一致，防止两边各漂各的。"""
    agents = _contract_endpoints(
        (REPO / "AGENTS.md").read_text(encoding="utf-8"), "服务接口约定"
    )
    design = _contract_endpoints(
        (REPO / "docs/design.md").read_text(encoding="utf-8"), "服务契约"
    )
    if not agents or not design:
        problems.append(
            "契约端点集合没抓到：AGENTS.md 或 design.md 的章节标题变了，检查 docs_check.py 的解析"
        )
        return
    if agents != design:
        for missing in sorted(design - agents):
            problems.append(f"AGENTS.md 契约缺 {missing}（design.md 有）")
        for extra in sorted(agents - design):
            problems.append(f"AGENTS.md 契约多出 {extra}（design.md 没有）")


# 历史解释豁免：讲"当初为什么这么做、后来为什么改"的行允许出现旧词，
# 标记约定是行内含「最初」/「曾」/「以前」/「旧口径」之一。
HISTORY_MARKERS = ("最初", "曾", "以前", "旧口径", "已下线")


def check_stale(files: list[Path], problems: list[str]) -> None:
    for path in files:
        rel = path.relative_to(REPO)
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if any(marker in line for marker in HISTORY_MARKERS):
                continue
            for pattern, reason in STALE_PATTERNS:
                if re.search(pattern, line):
                    problems.append(
                        f"{rel}:{line_number}: 「{line.strip()[:60]}」— {reason}"
                    )


# adding-a-service.md 用 forecast 举例讲解新增流程，属于文档示例
EXAMPLE_SERVICES = {"forecast"}


def check_services(files: list[Path], problems: list[str]) -> None:
    real = existing_services() | EXAMPLE_SERVICES
    for path in files:
        rel = path.relative_to(REPO)
        for match in SERVICE_RE.finditer(path.read_text(encoding="utf-8")):
            name = match.group(1)
            if name not in real:
                problems.append(
                    f"{rel}: 提到不存在的服务 modelman-{name}（services/ 下只有 {sorted(existing_services())}）"
                )


def main() -> int:
    files = doc_files()
    problems: list[str] = []
    check_links(files, problems)
    check_contract(files, problems)
    check_stale(files, problems)
    check_services(files, problems)

    if problems:
        print(f"文档检查未通过（{len(problems)} 处）：\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\n规则说明与新增规则的位置见 tools/docs_check.py 头部注释。",
            file=sys.stderr,
        )
        return 1
    print(f"文档检查通过（{len(files)} 份文档：链接、契约一致性、过期口径、服务名）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
