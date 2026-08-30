from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from interfaces import dashboard_api, host_ingress

requires_unix_socket = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="Unix socket chỉ được kiểm tra trên hệ điều hành có AF_UNIX.",
)


def _request(address: tuple[str, int], request: bytes) -> bytes:
    with socket.create_connection(address, timeout=2.0) as client:
        client.sendall(request)
        response = bytearray()
        while chunk := client.recv(4096):
            response.extend(chunk)
    return bytes(response)


@requires_unix_socket
def test_dashboard_api_serves_health_over_unix_socket(tmp_path: Path) -> None:
    path = tmp_path / "vision.sock"
    server = dashboard_api._unix_server(path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(path))
            client.sendall(b"GET /health/live HTTP/1.1\r\nHost: vision\r\nConnection: close\r\n\r\n")
            response = bytearray()
            while chunk := client.recv(4096):
                response.extend(chunk)
        assert b"HTTP/1.1 200 OK" in response
        assert b'{"status":"live"}' in response
        assert path.stat().st_mode & 0o777 == 0o660
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_lenode_routes_to_tbox_upstream(tmp_path: Path) -> None:
    received: list[bytes] = []
    ready = threading.Event()

    def tbox_upstream() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as upstream:
            upstream.bind(("127.0.0.1", 0))
            address.append(upstream.getsockname())
            upstream.listen(1)
            ready.set()
            connection, _address = upstream.accept()
            with connection:
                request = connection.recv(4096)
                received.append(request)
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}"
                )

    address: list[tuple[str, int]] = []
    upstream_thread = threading.Thread(target=tbox_upstream, daemon=True)
    upstream_thread.start()
    assert ready.wait(2.0)
    handler = type(
        "TestRoutingHandler",
        (host_ingress.HostRoutingHandler,),
        {
            "vision_unix_socket": str(tmp_path / "missing.sock"),
            "default_upstream": address[0],
        },
    )
    ingress = host_ingress.ThreadingIngress(("127.0.0.1", 0), handler)
    ingress_thread = threading.Thread(target=ingress.serve_forever, daemon=True)
    ingress_thread.start()
    try:
        response = _request(
            ingress.server_address,
            b"GET /vision/api/v1/streams?fresh=1 HTTP/1.1\r\n"
            b"Host: lenode.local\r\nConnection: close\r\n\r\n",
        )
        assert b"HTTP/1.1 200 OK" in response
        assert received
        assert received[0].startswith(b"GET /vision/api/v1/streams?fresh=1 HTTP/1.1\r\n")
    finally:
        ingress.shutdown()
        ingress.server_close()
        ingress_thread.join(timeout=2.0)
        upstream_thread.join(timeout=2.0)


@requires_unix_socket
def test_missing_vision_socket_returns_bad_gateway(tmp_path: Path) -> None:
    handler = type(
        "TestRoutingHandler",
        (host_ingress.HostRoutingHandler,),
        {
            "vision_unix_socket": str(tmp_path / "missing.sock"),
            "default_upstream": ("127.0.0.1", 1),
        },
    )
    ingress = host_ingress.ThreadingIngress(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=ingress.serve_forever, daemon=True)
    thread.start()
    try:
        response = _request(
            ingress.server_address,
            b"GET /dashboard.html HTTP/1.1\r\nHost: vision.local\r\nConnection: close\r\n\r\n",
        )
        assert b"HTTP/1.1 502 Bad Gateway" in response
    finally:
        ingress.shutdown()
        ingress.server_close()
        thread.join(timeout=2.0)
