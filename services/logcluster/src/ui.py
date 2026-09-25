"""自带 Web 界面。

三份静态文件（`src/static/`）在**导入时**读进内存。Python 没有 `include_str!`
那种编译期嵌入，用"启动时读"换同样的效果：文件缺失就是启动即失败，而不是运行时
404；代价是改页面要重启进程，而页面本来就不该在运行时改（它随镜像交付）。

文件放在 `src/` 下面是有意的：`Dockerfile` 只 `COPY services/logcluster/src`，
放外面不会进镜像。这一点由 `tests/test_http.py` 的页面用例兜住。

设计依据、交互约定与安全取舍见 `docs/logcluster-ui.md`。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import Response

_STATIC_DIR = Path(__file__).resolve().parent / "static"

INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_CSS = (_STATIC_DIR / "app.css").read_text(encoding="utf-8")
APP_JS = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

# 与 OCR 页面同款：`default-src 'none'` 起手，只放开同源资源。
# 差别只有 `img-src`：这个页面一张图都没有，就收紧到 `'none'`，
# 而不是跟着那边一起放开 `blob:`/`data:`。
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "img-src 'none'; "
    "connect-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def asset(body: str, media_type: str) -> Response:
    """页面的三条路由共用同一个响应形状。"""
    return Response(
        content=body,
        media_type=f"{media_type}; charset=utf-8",
        headers={
            # 换镜像后浏览器拿到旧页面，比多几个字节的请求麻烦得多
            "Cache-Control": "no-cache",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        },
    )
