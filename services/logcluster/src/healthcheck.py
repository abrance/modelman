"""容器健康检查用的自探活。

镜像里不装 curl/wget，所以用标准库直接发一个 HTTP/1.1 GET。做成独立模块而不是
给 main 加参数，是为了让 healthcheck 的依赖面尽可能小（不需要 fastapi 起得来）。

**探的是 `/readyz` 而不是 `/healthz`。** 状态文件不可用时进程仍在监听、
`/healthz` 照样 200，但业务端点一律 503；把健康状态绑在 `/healthz` 上会让容器
显示 healthy 而实际不可用，部署脚本的健康门也就形同虚设。用 `HEALTHCHECK_PATH`
可以改，但默认值就是想让"容器健康"等于"能提供服务"。
"""

from __future__ import annotations

import os
import socket
import sys

from .config import DEFAULT_LISTEN_ADDR, ConfigError, parse_listen_addr

TIMEOUT_SECS = 5.0

DEFAULT_PATH = "/readyz"


def probe(listen_addr: str, path: str = DEFAULT_PATH) -> int:
    try:
        host, port = parse_listen_addr(listen_addr)
    except ConfigError as exc:
        print(f"healthcheck: {exc}", file=sys.stderr)
        return 1

    target_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection(
            (target_host, port), timeout=TIMEOUT_SECS
        ) as sock:
            sock.settimeout(TIMEOUT_SECS)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {target_host}\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))

            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        print(f"healthcheck: {target_host}:{port} 不可达: {exc}", file=sys.stderr)
        return 1

    response = b"".join(chunks)
    status_line = response.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200" not in status_line:
        print(f"healthcheck: 意外状态行 {status_line!r}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    listen_addr = os.environ.get("LISTEN_ADDR", DEFAULT_LISTEN_ADDR)
    path = os.environ.get("HEALTHCHECK_PATH", DEFAULT_PATH)
    raise SystemExit(probe(listen_addr, path))


if __name__ == "__main__":
    main()
