"""Serve development mock MP4 files from a process isolated from the DMS API."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


class MockMediaHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    media_root = Path(".")

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _serve(self, *, send_body: bool) -> None:
        filename = unquote(urlparse(self.path).path.lstrip("/"))
        if not filename or filename != Path(filename).name or Path(filename).suffix.lower() != ".mp4":
            self.send_error(404)
            return
        path = self.media_root / filename
        if not path.is_file():
            self.send_error(404)
            return

        size = path.stat().st_size
        start = 0
        end = size - 1
        status = 200
        requested_range = self.headers.get("Range", "")
        if requested_range.startswith("bytes="):
            try:
                raw_start, raw_end = requested_range[6:].split("-", 1)
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else end
                elif raw_end:
                    start = max(0, size - int(raw_end))
                if start < 0 or start >= size or end < start:
                    raise ValueError
                end = min(end, size - 1)
                status = 206
            except (ValueError, OverflowError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        stat = path.stat()
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("ETag", f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"')
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body:
            return
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"mock media root does not exist: {root}")
    MockMediaHandler.media_root = root
    server = ThreadingHTTPServer((args.host, args.port), MockMediaHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
