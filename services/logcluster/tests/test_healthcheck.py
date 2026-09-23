"""自探活：容器健康检查走的就是它。

用真的 socket 服务来测，因为这里要验证的正是"状态行是否被正确判定"，
把 HTTP 解析换成 mock 就什么都没测到。
"""

from __future__ import annotations

import socket
import threading

from src.healthcheck import DEFAULT_PATH, probe


def _serve(response: bytes) -> tuple[str, int, dict, threading.Thread]:
    """起一个只回一条固定响应、并记下请求内容的 socket 服务。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    seen: dict = {}

    def run() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            seen["request"] = conn.recv(4096).decode("latin-1")
            conn.sendall(response)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return host, port, seen, thread


def _free_port() -> int:
    """拿一个刚被释放、大概率没人监听的端口。"""
    probe_socket = socket.socket()
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    return port


def test_probe_succeeds_on_200():
    host, port, seen, thread = _serve(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
    assert probe(f"{host}:{port}") == 0
    thread.join(timeout=5)
    assert "GET /readyz HTTP/1.1" in seen["request"]


def test_default_path_is_readiness_not_liveness():
    """健康检查必须绑在 /readyz 上：状态不可用时 /healthz 仍然是 200。"""
    assert DEFAULT_PATH == "/readyz"


def test_probe_fails_on_503():
    host, port, _, thread = _serve(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
    assert probe(f"{host}:{port}") == 1
    thread.join(timeout=5)


def test_probe_fails_when_nothing_listens():
    assert probe(f"127.0.0.1:{_free_port()}") == 1


def test_probe_fails_on_malformed_listen_addr():
    assert probe("8080") == 1


def test_probe_uses_the_given_path():
    host, port, seen, thread = _serve(b"HTTP/1.1 200 OK\r\n\r\n")
    assert probe(f"{host}:{port}", "/healthz") == 0
    thread.join(timeout=5)
    assert "GET /healthz HTTP/1.1" in seen["request"]
