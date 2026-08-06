import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRIGATE_URL = os.getenv("FRIGATE_URL", "http://frigate:5000").rstrip("/")
EVENT_PATH = re.compile(r"^/api/events/([A-Za-z0-9._-]+)/snapshot\.jpg$")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        match = EVENT_PATH.fullmatch(self.path)
        if not match:
            self.send_error(404)
            return
        try:
            url = f"{FRIGATE_URL}/api/events/{match.group(1)}/snapshot.jpg"
            with urllib.request.urlopen(url, timeout=15) as response:
                content = response.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_error(404)

    def log_message(self, *_):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("MEDIA_GATEWAY_PORT", "8090"))), Handler).serve_forever()
