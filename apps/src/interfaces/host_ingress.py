from __future__ import annotations

import argparse
import socket
import socketserver
import threading

MAX_HEADER_BYTES = 64 * 1024


class HostRoutingHandler(socketserver.BaseRequestHandler):
    vision_unix_socket: str
    default_upstream: tuple[str, int]

    def handle(self) -> None:
        self.request.settimeout(10.0)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > MAX_HEADER_BYTES:
                return
        host = ""
        for line in bytes(header).split(b"\r\n")[1:]:
            name, separator, value = line.partition(b":")
            if separator and name.strip().lower() == b"host":
                host = value.strip().decode("ascii", errors="ignore").split(":", 1)[0].lower()
                break
        use_vision = host == "vision.local"
        try:
            if use_vision:
                unix_family = getattr(socket, "AF_UNIX", None)
                if unix_family is None:
                    raise OSError("Unix sockets are unavailable on this platform")
                upstream = socket.socket(unix_family, socket.SOCK_STREAM)
                upstream.settimeout(5.0)
                upstream.connect(self.vision_unix_socket)
            else:
                upstream = socket.create_connection(self.default_upstream, timeout=5.0)
        except OSError:
            self.request.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            return
        with upstream:
            upstream.sendall(header)
            self.request.settimeout(None)
            upstream.settimeout(None)
            client_to_upstream = threading.Thread(
                target=self._copy,
                args=(self.request, upstream),
                daemon=True,
            )
            client_to_upstream.start()
            self._copy(upstream, self.request)

    @staticmethod
    def _copy(source: socket.socket, destination: socket.socket) -> None:
        try:
            while chunk := source.recv(64 * 1024):
                destination.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass


class ThreadingIngress(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Host-routing ingress for LS-Vision and T-Box")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--vision-unix-socket", default="/run/ls-vision/api.sock")
    parser.add_argument("--default-upstream", default="127.0.0.1:8000")
    args = parser.parse_args()

    def address(value: str) -> tuple[str, int]:
        host, port = value.rsplit(":", 1)
        return host, int(port)

    handler = type(
        "ConfiguredHostRoutingHandler",
        (HostRoutingHandler,),
        {
            "vision_unix_socket": args.vision_unix_socket,
            "default_upstream": address(args.default_upstream),
        },
    )
    with ThreadingIngress((args.host, args.port), handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
