#!/usr/bin/env python3
"""用真实浏览器跑一遍日志聚类页面（`/`）的验收清单。

把 `docs/logcluster-ui.md` 的验收项变成可重跑的脚本。

它不是 CI 的一部分，也不在 `requirements-dev.txt` 里：跑之前自己装一次

    python3 -m pip install playwright && python3 -m playwright install chromium

场景（先按需把服务跑起来）：

    core    贴日志 → 聚类 → 看模板 → 只匹配 → 空行提示 → 布局。默认场景。
    limits  需要实例的 MAX_LINES 小到会拒绝默认样本；验超限时按钮被禁用。
    auth    需要实例设了 AUTH_TOKEN；验 401 提示与填对 token 后可用。

例子：

    python3 tools/ui_acceptance.py --base http://127.0.0.1:8080
    python3 tools/ui_acceptance.py --base http://127.0.0.1:8081 --scenario limits
    python3 tools/ui_acceptance.py --base http://127.0.0.1:8082 --scenario auth --auth-token secret

注意这个脚本会**真的往模板树里写数据**（「聚类」按钮就是写接口），所以别拿它去
打一个正在用的实例；要验线上就自己挑一台或接受模板树里多几条。

退出码 0 表示该场景全部通过。

实现注意：JS 一律写成函数形式（`"() => …"`）。裸表达式在页面的严格 CSP
（`script-src 'self'`）下会被 Chrome 拒绝执行，报 "Evaluating a string as
JavaScript violates CSP" —— 这正是页面不带 `unsafe-eval` 的代价。
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

LINES = [
    "user alice logged in from 10.0.0.1",
    "user bob logged in from 10.0.0.2",
    "disk usage 95% on /dev/sda1",
]
NEW_LINES = "kernel: out of memory: killed process 4242 (chrome)"
TIMEOUT_MS = 30_000

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    suffix: str | None = f"  [{detail}]" if detail else None
    print(("PASS " if ok else "FAIL ") + name + (suffix or ""))


def open_page(
    browser: Browser, base: str, token: str = "", viewport: dict | None = None
) -> Page:
    page = browser.new_page(viewport=viewport or {"width": 390, "height": 844})
    page.goto(f"{base}/", wait_until="networkidle")
    if token:
        page.evaluate("() => { document.getElementById('token-box').open = true; }")
        page.fill("#token-input", token)
        page.locator("#token-input").press("Tab")
        page.wait_for_timeout(300)
    return page


def paste(page: Page, text: str) -> None:
    page.fill("#lines", text)


def summary(page: Page) -> str:
    return page.locator("#summary").inner_text()


def template_rows(page: Page) -> int:
    return page.locator("#templates li").count()


def wait_idle(page: Page) -> None:
    page.wait_for_function(
        "() => !document.getElementById('cluster').disabled",
        timeout=TIMEOUT_MS,
    )


def scenario_core(browser: Browser, args: argparse.Namespace) -> None:
    context: BrowserContext = browser.new_context(
        viewport={"width": 390, "height": 844}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on(
        "console", lambda msg: errors.append(msg.text) if msg.type == "error" else None
    )
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(f"{args.base}/", wait_until="networkidle")

    check("页面标题", "日志模板聚类" in page.title(), page.title())
    check(
        "状态条读到了生效参数",
        "sim_th" in page.locator("#stat-params").inner_text(),
        page.locator("#stat-params").inner_text(),
    )
    check(
        "状态条读到模板数与覆盖行数",
        "模板" in page.locator("#stat-counts").inner_text(),
        page.locator("#stat-counts").inner_text(),
    )

    # ── 聚类（写） ──────────────────────────────────────────────────
    paste(page, "\n".join(LINES))
    check(
        "预算是按行数与字节数算的",
        "3 行" in page.locator("#budget").inner_text(),
        page.locator("#budget").inner_text(),
    )
    page.click("#cluster")
    page.wait_for_function(
        "() => document.querySelectorAll('#results li').length >= 3",
        timeout=TIMEOUT_MS,
    )
    wait_idle(page)

    text = summary(page)
    check("汇总给出处理行数", "处理 3 行" in text, text)
    check("汇总给出新建模板数", "本次新建" in text, text)
    check("逐行结果各一行", page.locator("#results li").count() >= 3)
    # 注意：新建簇那一条的 template 就是日志原文本身（服务端 Drain3 的行为，
    # 参数要等下一条把模板改写出来），所以不能拿第一条断言，只能要求在结果里
    # 至少出现一条带 <*> 的模板。
    templates = page.locator("#results li .tpl").all_inner_texts()
    check(
        "结果里回显了模板",
        any("<*>" in item for item in templates),
        " | ".join(templates),
    )
    check(
        "新建模板的行被标记出来",
        page.locator("#results li.changed").count() >= 2,
        str(page.locator("#results li.changed").count()),
    )
    check(
        "行内显示了簇 id",
        page.locator("#results li .tag").first.inner_text().startswith("#"),
        page.locator("#results li .tag").first.inner_text(),
    )

    # ── 模板面板（读） ──────────────────────────────────────────────
    page.click("#refresh")
    page.wait_for_function(
        "() => document.querySelectorAll('#templates li').length >= 2",
        timeout=TIMEOUT_MS,
    )
    check("模板面板列出模板", template_rows(page) >= 2, str(template_rows(page)))
    check(
        "模板面板给出命中次数",
        "行" in page.locator("#templates li .tag").nth(1).inner_text(),
        page.locator("#templates li .tag").nth(1).inner_text(),
    )

    # ── 只匹配（读） ────────────────────────────────────────────────
    page.click("#match")
    page.wait_for_function(
        "() => (document.getElementById('summary').innerText || '').includes('只匹配')",
        timeout=TIMEOUT_MS,
    )
    wait_idle(page)
    text = summary(page)
    check("只匹配标注了未写入", "未写入" in text, text)
    check("只匹配给出命中行数", "命中 3 行" in text, text)

    # 全新的一行应当匹配不上（模板树里没有）
    paste(page, NEW_LINES)
    page.click("#match")
    page.wait_for_function(
        "() => (document.getElementById('summary').innerText || '').includes('只匹配')",
        timeout=TIMEOUT_MS,
    )
    wait_idle(page)
    check("未命中被标出来", "未命中" in summary(page), summary(page))

    # ── 输入的边界 ──────────────────────────────────────────────────
    paste(page, "\n".join(LINES))
    paste(page, "\n".join(LINES) + "\n\n\n")
    page.click("#cluster")
    page.wait_for_timeout(800)
    wait_idle(page)
    check(
        "空行被忽略并提示",
        "忽略了" in page.locator("#notice").inner_text(),
        page.locator("#notice").inner_text(),
    )

    # 模板文本要走 textContent：贴一段像标签的日志，看它不会被解析成元素
    paste(page, "<img src=x onerror=alert(1)> template probe")
    page.click("#cluster")
    page.wait_for_timeout(1200)
    wait_idle(page)
    injected = page.locator("#results li .line img").count()
    check("日志原文没有被当成 HTML 渲染", injected == 0, f"img 元素 {injected} 个")

    check("无 console 报错", not errors, "; ".join(errors[:2]))

    # ── 布局 ────────────────────────────────────────────────────────
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check("手机宽度无横向溢出", overflow <= 1, f"溢出 {overflow}px")

    wide = context.new_page()
    wide.set_viewport_size({"width": 1280, "height": 900})
    wide.goto(f"{args.base}/", wait_until="networkidle")
    columns = wide.evaluate(
        "() => getComputedStyle(document.querySelector('.grid')).gridTemplateColumns"
    )
    check("宽屏下输入与结果并排", len(columns.split()) >= 2, columns)
    wide_overflow = wide.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check("宽屏无横向溢出", wide_overflow <= 1, f"溢出 {wide_overflow}px")
    context.close()


def scenario_limits(browser: Browser, args: argparse.Namespace) -> None:
    """需要实例的 MAX_LINES 小到会拒绝 core 场景那批日志。"""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(f"{args.base}/", wait_until="networkidle")

    lines = "\n".join(f"line {index} from host-1" for index in range(1, 40))
    paste(page, lines)
    check(
        "超行数时按钮被禁用",
        page.locator("#cluster").is_disabled() and page.locator("#match").is_disabled(),
    )
    check(
        "超行数时给出原因",
        "超过上限" in page.locator("#notice").inner_text(),
        page.locator("#notice").inner_text(),
    )

    paste(page, "\n".join(LINES))
    check(
        "回到上限内按钮恢复可用",
        not page.locator("#cluster").is_disabled(),
    )

    too_long = "x" * 20_000
    paste(page, too_long)
    check(
        "单行超长时点名是第几行",
        "第 1 行长度" in page.locator("#notice").inner_text(),
        page.locator("#notice").inner_text()[:60],
    )
    context.close()


def scenario_auth(browser: Browser, args: argparse.Namespace) -> None:
    """需要实例设了 AUTH_TOKEN，且 --auth-token 与它一致。"""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(f"{args.base}/", wait_until="networkidle")

    # 页面本身必须免鉴权，否则连填 token 的机会都没有
    check("页面在启用鉴权时仍然打开", "日志模板聚类" in page.title())

    paste(page, "\n".join(LINES))
    page.click("#cluster")
    page.wait_for_function(
        "() => (document.getElementById('notice').innerText || '').includes('token')",
        timeout=TIMEOUT_MS,
    )
    check(
        "没填 token 时提示要 token",
        "token" in page.locator("#notice").inner_text(),
        page.locator("#notice").inner_text(),
    )
    check("token 输入框自动展开", page.locator("#token-box").evaluate("el => el.open"))
    context.close()

    authed = open_page(browser, args.base, token=args.auth_token)
    paste(authed, "\n".join(LINES))
    authed.click("#cluster")
    authed.wait_for_function(
        "() => (document.getElementById('summary').innerText || '').includes('处理')",
        timeout=TIMEOUT_MS,
    )
    check("填对 token 后聚类成功", "处理 3 行" in summary(authed), summary(authed))
    authed.context.close()


SCENARIOS = {
    "core": scenario_core,
    "limits": scenario_limits,
    "auth": scenario_auth,
}


def run(args: argparse.Namespace) -> int:
    names = [item.strip() for item in args.scenario.split(",") if item.strip()]
    if "all" in names:
        names = list(SCENARIOS)

    for name in names:
        handler = SCENARIOS.get(name)
        if handler is None:
            print(f"未知场景：{name}", file=sys.stderr)
            return 2
        print(f"── 场景 {name} ──")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                handler(browser, args)
            finally:
                browser.close()
        print()

    failed = [name for name, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="日志聚类页面验收（需要 playwright 与一个在跑的服务）"
    )
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="服务地址")
    parser.add_argument(
        "--scenario", default="core", help="core / limits / auth / all，可逗号分隔"
    )
    parser.add_argument("--auth-token", default="", help="该实例设置的 AUTH_TOKEN")
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
