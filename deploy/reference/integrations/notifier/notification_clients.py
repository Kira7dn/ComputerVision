"""Dependency-free Telegram and Zalo Bot API clients."""
import json
import os
import time
import urllib.error
import urllib.request

RETRY_COUNT = max(1, int(os.getenv("NOTIFIER_RETRY_COUNT", "3")))
RETRY_BACKOFF = float(os.getenv("NOTIFIER_RETRY_BACKOFF_SECONDS", "2"))


def _post_json(url, payload):
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    last = None
    for attempt in range(RETRY_COUNT):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
            if not result.get("ok", True):
                raise RuntimeError("provider rejected request")
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < RETRY_COUNT:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise last


def _post_multipart(url, fields, content):
    boundary = "----FrigateNotifierBoundary"
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snapshot.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(url, data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    last = None
    for attempt in range(RETRY_COUNT):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
            if not result.get("ok", True):
                raise RuntimeError("provider rejected photo")
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < RETRY_COUNT:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise last


class TelegramClient:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send_message(self, text):
        if not self.enabled:
            return False
        _post_json(f"https://api.telegram.org/bot{self.token}/sendMessage", {"chat_id": self.chat_id, "text": text})
        return True

    def send_photo(self, content, caption):
        if not self.enabled:
            return False
        _post_multipart(f"https://api.telegram.org/bot{self.token}/sendPhoto", {"chat_id": self.chat_id, "caption": caption}, content)
        return True


class ZaloClient:
    def __init__(self):
        self.token = os.getenv("ZALO_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("ZALO_CHAT_ID", "").strip()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send_message(self, text):
        if not self.enabled:
            return False
        _post_json(f"https://bot-api.zaloplatforms.com/bot{self.token}/sendMessage", {"chat_id": self.chat_id, "text": text})
        return True

    def send_photo(self, photo_url, caption):
        if not self.enabled:
            return False
        _post_json(f"https://bot-api.zaloplatforms.com/bot{self.token}/sendPhoto", {"chat_id": self.chat_id, "photo": photo_url, "caption": caption})
        return True
