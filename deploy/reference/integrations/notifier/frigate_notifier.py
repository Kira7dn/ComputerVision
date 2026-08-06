"""Durable Frigate event -> vehicle contract -> notification adapter."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from notification_clients import TelegramClient, ZaloClient

FRIGATE_URL = os.getenv("FRIGATE_URL", "http://frigate:5000").rstrip("/")
POLL_SECONDS = float(os.getenv("NOTIFIER_POLL_SECONDS", "3"))
RETRY_COUNT = max(1, int(os.getenv("NOTIFIER_RETRY_COUNT", "3")))
RETRY_BACKOFF = float(os.getenv("NOTIFIER_RETRY_BACKOFF_SECONDS", "2"))
MIN_TRACK_DISPLACEMENT = float(os.getenv("MIN_TRACK_DISPLACEMENT", "0.12"))
MIN_TRACK_DURATION_SECONDS = float(os.getenv("MIN_TRACK_DURATION_SECONDS", "2"))
WHITELIST_FILE = Path(os.getenv("VEHICLE_WHITELIST_FILE", "E:/Docker/Frigate/runtime/vehicle_whitelist.json"))
STATE_FILE = Path(os.getenv("NOTIFIER_STATE_FILE", "E:/Docker/Frigate/runtime/notifier_state.json"))
ZALO_WEBHOOK = os.getenv("ZALO_WEBHOOK_URL", "").strip()
ZALO_MEDIA_BASE_URL = (os.getenv("ZALO_MEDIA_BASE_URL") or os.getenv("NGROK_URL", "")).rstrip("/")


def get_json(path):
    with urllib.request.urlopen(FRIGATE_URL + path, timeout=10) as response:
        return json.load(response)


def _read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


def normalize_plate(value):
    return "".join(str(value or "").upper().split()).replace("-", "").replace(".", "")


def load_whitelist(path=WHITELIST_FILE):
    data = _read_json(path, [])
    return data.get("vehicles", []) if isinstance(data, dict) else data


def classify_plate(plate, entries):
    if not plate:
        return "unreadable"
    for entry in entries:
        if normalize_plate(entry.get("plate")) == plate and entry.get("enabled", True):
            return "allowed"
    return "not_allowed"


def _plate_from_event(event):
    data = event.get("data") or {}
    return normalize_plate(data.get("recognized_license_plate") or event.get("recognized_license_plate"))


def track_displacement(event):
    """Return normalized center movement from Frigate path_data."""
    path = (event.get("data") or {}).get("path_data") or []
    points = [(float(item[0][0]), float(item[0][1])) for item in path if isinstance(item, list) and len(item) >= 1 and isinstance(item[0], list) and len(item[0]) >= 2]
    if len(points) < 2:
        return 0.0
    return max(max(p[0] for p in points) - min(p[0] for p in points), max(p[1] for p in points) - min(p[1] for p in points))


def is_vehicle_moving(event):
    """Accept only tracks with meaningful spatial movement.

    Frigate's estimated speed can be non-zero because of bbox jitter while a
    vehicle is stationary. It is therefore telemetry, not an independent
    movement decision. Require both a minimum track duration and normalized
    path displacement so parked/queued vehicles do not trigger business
    events or notifications.
    """
    data = event.get("data") or {}
    start = event.get("start_time")
    end = event.get("end_time")
    duration = float(end) - float(start) if start and end else 0
    return duration >= MIN_TRACK_DURATION_SECONDS and track_displacement(event) >= MIN_TRACK_DISPLACEMENT


def normalize_event(event, frigate_url=FRIGATE_URL, whitelist=None):
    camera = event.get("camera", "unknown")
    direction = {"gate_in_camera": "in", "gate_out_camera": "out"}.get(camera)
    if not direction or event.get("label") != "car":
        return None
    if not is_vehicle_moving(event):
        return None
    event_id = event.get("id")
    plate = _plate_from_event(event)
    started = event.get("start_time")
    ended = event.get("end_time")
    def iso(value):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value))) if value else None
    return {
        "event_id": event_id,
        "camera": camera,
        "direction": direction,
        "vehicle_type": "car",
        "moving": True,
        "track_displacement": round(track_displacement(event), 4),
        "plate": plate or None,
        "plate_confidence": (event.get("data") or {}).get("recognized_license_plate_score") or event.get("recognized_license_plate_score"),
        "plate_status": "recognized" if plate else "unreadable",
        "started_at": iso(started),
        "ended_at": iso(ended),
        "snapshot_url": f"{frigate_url}/api/events/{urllib.parse.quote(event_id or '')}/snapshot.jpg",
        "clip_url": f"{frigate_url}/api/events/{urllib.parse.quote(event_id or '')}/clip.mp4",
        "plate_result": classify_plate(plate, whitelist if whitelist is not None else load_whitelist()),
    }


def _request_with_retry(request):
    last = None
    for attempt in range(RETRY_COUNT):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt + 1 < RETRY_COUNT:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise last


def _multipart(url, fields, file_field, filename, content, content_type):
    boundary = "----FrigateNotifierBoundary"
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(url, data=bytes(body), method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _request_with_retry(request)


def send_telegram(event):
    client = TelegramClient()
    if not client.enabled:
        return False
    snapshot = urllib.request.urlopen(event["snapshot_url"], timeout=15).read()
    caption = "Xe {} | {} | biển số: {} | {}".format(event["direction"], event["camera"], event["plate"] or "unreadable", event["plate_result"])
    return client.send_photo(snapshot, caption)


def send_zalo(event):
    client = ZaloClient()
    caption = "Xe {} | {} | biển số: {} | {}".format(event["direction"], event["camera"], event["plate"] or "unreadable", event["plate_result"])
    if client.enabled and ZALO_MEDIA_BASE_URL:
        photo_url = ZALO_MEDIA_BASE_URL + "/api/events/{}/snapshot.jpg".format(event["event_id"])
        return client.send_photo(photo_url, caption)
    if not ZALO_WEBHOOK:
        return False
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(ZALO_WEBHOOK, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    return 200 <= _request_with_retry(request) < 300


def main():
    print("frigate notifier started", flush=True)
    while True:
        try:
            state = _read_json(STATE_FILE, {})
            for raw in reversed(get_json("/api/events?limit=50")):
                if not raw.get("end_time"):
                    continue
                event = normalize_event(raw)
                if not event or not event["event_id"]:
                    continue
                record = state.setdefault(event["event_id"], {})
                sent = []
                for name, sender in (("telegram", send_telegram), ("zalo", send_zalo)):
                    if not record.get(name):
                        try:
                            if sender(event):
                                record[name] = time.time(); sent.append(name)
                        except Exception as exc:
                            print("{} notification failed for {}: {}".format(name, event["event_id"], type(exc).__name__), flush=True)
                if sent:
                    _write_json(STATE_FILE, state)
                    print("notification sent: {} {}".format(event["event_id"], ",".join(sent)), flush=True)
        except Exception as exc:
            print("notification cycle failed: {}".format(type(exc).__name__), flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
