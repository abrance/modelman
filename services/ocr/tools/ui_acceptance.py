#!/usr/bin/env python3
"""用真实浏览器跑一遍 OCR 页面（`/`）的验收清单。

把 `docs/ocr-ui.md` 的「手动验收」变成可重跑的脚本。

它不是 CI 的一部分，也不在 `requirements-dev.txt` 里：跑之前自己装一次

    python3 -m pip install playwright && python3 -m playwright install chromium

场景（先按需把服务跑起来，`make run SERVICE=ocr`）：

    core       单图/多图/粘贴/复制/导出/位置框/历史/布局。默认场景。
    auth       需要实例设了 AUTH_TOKEN；验 401 提示与填对 token 后成功。
    too-large  需要实例的 MAX_IMAGE_BYTES 小到会拒绝默认样本；验超限提示与压缩重试。

例子：

    python3 tools/ui_acceptance.py --base http://127.0.0.1:8080
    python3 tools/ui_acceptance.py --base http://127.0.0.1:8081 --scenario auth --auth-token secret
    python3 tools/ui_acceptance.py --base http://127.0.0.1:8082 --scenario too-large

退出码 0 表示该场景全部通过。

实现注意：JS 一律写成函数形式（`"() => …"`）。裸表达式在页面的严格 CSP
（`script-src 'self'`）下会被 Chrome 拒绝执行，报 "Evaluating a string as
JavaScript violates CSP" —— 这正是页面不带 `unsafe-eval` 的代价。
"""

from __future__ import annotations

import argparse
import base64
import pathlib
import sys

from playwright.sync_api import Browser, Page, sync_playwright

SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = SERVICE_ROOT / "tests" / "fixtures" / "case_05.png"
# case_05 是契约基线里的样本，期望识别出这两个字
EXPECTED_TEXT = "烈战"
TIMEOUT_MS = 30_000

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    suffix = f"  [{detail}]" if detail else ""
    print(("PASS " if ok else "FAIL ") + name + suffix)


def image_payload(path: pathlib.Path) -> dict:
    return {"name": path.name, "mimeType": "image/png", "buffer": path.read_bytes()}


def set_token(page: Page, token: str) -> None:
    """token 输入框默认折叠，先展开再填 —— 否则 Playwright 会说不可见。"""
    page.evaluate("() => { document.getElementById('token-details').open = true; }")
    page.fill("#token-input", token)
    page.locator("#token-input").press("Tab")


def open_page(
    browser: Browser, base: str, token: str = "", viewport: dict | None = None
) -> Page:
    page = browser.new_page(viewport=viewport or {"width": 390, "height": 844})
    page.goto(f"{base}/", wait_until="networkidle")
    if token:
        set_token(page, token)
    return page


def wait_for_result(page: Page) -> None:
    page.wait_for_selector(".card .line", timeout=TIMEOUT_MS)


# ── 场景：core ─────────────────────────────────────────────────────────────


def scenario_core(browser: Browser, args: argparse.Namespace) -> None:
    payload = image_payload(args.fixture)
    not_an_image = {
        "name": "notes.txt",
        "mimeType": "text/plain",
        "buffer": b"not an image",
    }

    errors: list[str] = []
    page = open_page(browser, args.base)
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    check("页面标题", page.title() == "OCR 文字识别", page.title())

    options = page.locator("#model-select option").all_inner_texts()
    check("档位下拉由 /models 填充", len(options) >= 1, ",".join(options))

    page.set_input_files("#file-input", payload)
    wait_for_result(page)
    check("点选图片能出文字", EXPECTED_TEXT in page.locator(".card .text").inner_text())

    meta = page.locator(".card [data-role='meta']").inner_text()
    check("卡片显示耗时与置信度", "ms" in meta and "置信度" in meta, meta)

    boxes = page.locator(".box").count()
    lines = page.locator(".card .line").count()
    check(
        "位置框与文本行一一对应",
        boxes == lines and boxes >= 1,
        f"框 {boxes} / 行 {lines}",
    )

    page.locator(".card .line").first.click()
    check("点文本行高亮位置框", page.locator(".box.active").count() == 1)
    page.locator(".box").first.click()
    check("点位置框高亮文本行", page.locator(".line.active").count() == 1)

    page.reload(wait_until="networkidle")
    count = page.locator("#history-list .history-item").count()
    check("历史记录刷新后仍在", count >= 1, f"{count} 条")

    # 多图、非图片过滤、张数上限
    page.locator("#clear-btn").click()
    page.set_input_files("#file-input", [payload, payload, not_an_image])
    page.wait_for_function(
        "() => document.querySelector('#progress').textContent.includes('2/2')",
        timeout=TIMEOUT_MS,
    )
    check("多图：只处理图片", page.locator(".card").count() == 2)
    check("多图：提示忽略了非图片", "非图片" in page.locator("#banner").inner_text())

    page.locator("#clear-btn").click()
    page.set_input_files("#file-input", [payload] * 22)
    page.wait_for_function(
        "() => document.querySelectorAll('.card').length === 20", timeout=TIMEOUT_MS
    )
    check("一次最多 20 张", page.locator(".card").count() == 20)

    # 粘贴
    page.locator("#clear-btn").click()
    page.evaluate(
        """async (b64) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], 'pasted.png', { type: 'image/png' });
            const data = new DataTransfer();
            data.items.add(file);
            document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data }));
        }""",
        base64.b64encode(payload["buffer"]).decode(),
    )
    wait_for_result(page)
    check("粘贴图片能出文字", EXPECTED_TEXT in page.locator(".card .text").inner_text())

    check("无 console 报错", not errors, "; ".join(errors[:2]))
    check(
        "手机宽度无横向溢出",
        page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
        ),
    )

    # 复制与导出（剪贴板权限按 context 给）
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    clip = context.new_page()
    clip.goto(f"{args.base}/", wait_until="networkidle")
    clip.set_input_files("#file-input", payload)
    wait_for_result(clip)
    clip.locator("#copy-all").click()
    clip.wait_for_timeout(400)
    clipboard = clip.evaluate("() => navigator.clipboard.readText()")
    check("全部复制写入剪贴板", EXPECTED_TEXT in clipboard, clipboard[:20])

    with clip.expect_download() as download:
        clip.locator("#export-txt").click()
    exported = pathlib.Path(download.value.path()).read_text(encoding="utf-8")
    check(
        "导出 .txt 已按格式分块",
        exported.startswith("# ") and EXPECTED_TEXT in exported,
    )

    with clip.expect_download() as history_download:
        clip.locator("#export-history").click()
    history = pathlib.Path(history_download.value.path()).read_text(encoding="utf-8")
    check("导出历史已按格式分块", history.startswith("## "))

    # 宽屏布局
    wide = open_page(browser, args.base, viewport={"width": 1200, "height": 900})
    wide.set_input_files("#file-input", payload)
    wait_for_result(wide)
    zone = wide.locator("#dropzone").bounding_box()
    cards = wide.locator("#cards").bounding_box()
    check("宽屏下输入与结果并排", cards["x"] > zone["x"] + zone["width"] - 5)
    check(
        "宽屏无横向溢出",
        wide.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
        ),
    )


# ── 场景：auth ─────────────────────────────────────────────────────────────


def scenario_auth(browser: Browser, args: argparse.Namespace) -> None:
    if not args.auth_token:
        raise SystemExit("--scenario auth 需要同时给 --auth-token")
    payload = image_payload(args.fixture)

    # 新 context = 干净的 localStorage，等效于没填过 token 的新手机
    fresh = browser.new_context().new_page()
    fresh.goto(f"{args.base}/", wait_until="networkidle")
    fresh.set_input_files("#file-input", payload)
    fresh.wait_for_selector("#token-details[open]", timeout=TIMEOUT_MS)
    check(
        "401：自动展开 token 输入并标红",
        "invalid" in (fresh.locator("#token-input").get_attribute("class") or ""),
    )
    check(
        "401：卡片给出可读原因",
        "token" in fresh.locator(".card [data-role='error']").inner_text(),
        fresh.locator(".card [data-role='error']").inner_text(),
    )

    await_token = open_page(browser, args.base, token=args.auth_token)
    await_token.set_input_files("#file-input", payload)
    wait_for_result(await_token)
    check(
        "填对 token 后识别成功",
        EXPECTED_TEXT in await_token.locator(".card .text").inner_text(),
    )

    # 页面本身（含静态资源）必须免鉴权，否则手机连填 token 的机会都没有
    for path in ("/", "/app.css", "/app.js"):
        status = browser.new_context().request.get(f"{args.base}{path}").status
        check(f"{path} 免鉴权", status == 200, f"HTTP {status}")


# ── 场景：too-large ────────────────────────────────────────────────────────


def scenario_too_large(browser: Browser, args: argparse.Namespace) -> None:
    payload = image_payload(args.fixture)
    page = open_page(
        browser,
        args.base,
        token=args.auth_token,
        viewport={"width": 390, "height": 844},
    )
    page.set_input_files("#file-input", payload)
    page.wait_for_selector(".card [data-role='retry']", timeout=TIMEOUT_MS)

    message = page.locator(".card [data-role='error']").inner_text()
    check("超限：提示里带 MAX_IMAGE_BYTES", "MAX_IMAGE_BYTES" in message, message[:60])
    check(
        "超限：按钮是压缩后重试",
        page.locator(".card [data-role='retry']").inner_text() == "压缩后重试",
    )

    page.locator(".card [data-role='retry']").click()
    page.wait_for_timeout(3000)
    if page.locator(".card .line").count():
        meta = page.locator(".card [data-role='meta']").inner_text()
        check("超限：压缩后识别成功", "已压缩" in meta and "ms" in meta, meta)
    else:
        # 上限小到连压缩后的图也放不下：必须给明确交代，且不留下能无限点的按钮
        text = page.locator(".card [data-role='error']").inner_text()
        check("超限：仍超限时给出明确交代", "仍然超过上限" in text, text[:60])
        check(
            "超限：不再重复提供压缩按钮",
            page.locator(".card [data-role='retry']").is_hidden(),
        )


SCENARIOS = {
    "core": scenario_core,
    "auth": scenario_auth,
    "too-large": scenario_too_large,
}


def run(args: argparse.Namespace) -> int:
    wanted = list(SCENARIOS) if args.scenario == "all" else args.scenario.split(",")
    for name in wanted:
        if name not in SCENARIOS:
            raise SystemExit(f"未知场景 {name}；可选：{', '.join(SCENARIOS)}、all")
        print(f"── 场景 {name} ──")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                SCENARIOS[name](browser, args)
            finally:
                browser.close()
        print()

    failed = [name for name, ok in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OCR 页面验收（需要 playwright 与一个在跑的服务）"
    )
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="服务地址")
    parser.add_argument(
        "--scenario", default="core", help="core / auth / too-large / all，可逗号分隔"
    )
    parser.add_argument("--auth-token", default="", help="该实例设置的 AUTH_TOKEN")
    parser.add_argument(
        "--fixture", type=pathlib.Path, default=DEFAULT_FIXTURE, help="验收用样本图"
    )
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
