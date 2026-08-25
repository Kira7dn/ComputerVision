"""Telegram/Zalo delivery for the standalone DeepStream runtime.

This module owns the provider HTTP contracts for the standalone notification
outbox and deliberately has no dependency on another runtime.
Events are queued only after EvidenceStore has created the event artifact.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

LOG = logging.getLogger("ls-vision.notifications")
# Request URLs contain provider credentials, so the generic client request log
# must never be emitted into the operator's live log stream.
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class DeliveryResult:
    sent: bool
    retryable: bool = False
    error: str | None = None


@dataclass(frozen=True)
class Recipient:
    id: str
    chat_id: str


@dataclass(frozen=True)
class Provider:
    name: str
    enabled: bool
    token: str
    recipients: tuple[Recipient, ...]

    @property
    def configured(self) -> bool:
        return bool(self.token and self.recipients)


@dataclass(frozen=True)
class NotificationJob:
    idempotency_key: str
    event_id: str
    function: str
    camera: str
    lifecycle: str
    provider: Provider
    recipient: Recipient
    title: str
    message: str
    snapshot_path: Path | None
    snapshot_url: str | None


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _environment() -> dict[str, str]:
    env_file = os.environ.get("CAMERA_ENV_FILE")
    values = _dotenv_values(Path(env_file)) if env_file else {}
    values.update({key: value for key, value in os.environ.items() if value})
    return values


def _resolve(value: Any, environment: dict[str, str]) -> str:
    text = str(value or "").strip()
    if len(text) >= 3 and text.startswith("{") and text.endswith("}"):
        return environment.get(text[1:-1], "").strip()
    return text


def _response_result(response: httpx.Response) -> DeliveryResult:
    body: Any = {}
    try:
        body = response.json()
    except ValueError:
        pass
    if 200 <= response.status_code < 300 and not (
        isinstance(body, dict) and body.get("ok") is False
    ):
        return DeliveryResult(True)
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("description") or body.get("message") or "")
    if not detail:
        detail = response.text.strip()
    detail = " ".join(detail.split())[:240]
    error = f"HTTP {response.status_code}"
    if detail:
        error = f"{error}: {detail}"
    return DeliveryResult(
        False,
        response.status_code in (408, 425, 429) or response.status_code >= 500,
        error,
    )


def _text(title: str, message: str, snapshot_url: str | None) -> str:
    del snapshot_url
    return "\n".join(part.strip() for part in (title, message) if part and part.strip())[:4096]


def _deliver_telegram(
    client: httpx.Client, job: NotificationJob
) -> DeliveryResult:
    base = f"https://api.telegram.org/bot{job.provider.token}"
    text = _text(job.title, job.message, job.snapshot_url)
    try:
        if job.snapshot_path and job.snapshot_path.is_file():
            with job.snapshot_path.open("rb") as image:
                response = client.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": job.recipient.chat_id, "caption": text[:1024]},
                    files={"photo": ("snapshot.jpg", image, "image/jpeg")},
                )
        else:
            response = client.post(
                f"{base}/sendMessage",
                json={"chat_id": job.recipient.chat_id, "text": text},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as error:
        return DeliveryResult(False, True, type(error).__name__)
    return _response_result(response)


def _deliver_zalo(client: httpx.Client, job: NotificationJob) -> DeliveryResult:
    base = f"https://bot-api.zaloplatforms.com/bot{job.provider.token}"
    text = _text(job.title, job.message, job.snapshot_url)
    endpoint = "sendMessage"
    payload: dict[str, Any] = {
        "chat_id": job.recipient.chat_id,
        "text": text,
    }
    if job.snapshot_url:
        endpoint = "sendPhoto"
        payload = {
            "chat_id": job.recipient.chat_id,
            "photo": job.snapshot_url,
            "caption": text,
        }
    try:
        response = client.post(f"{base}/{endpoint}", json=payload)
    except (httpx.TimeoutException, httpx.NetworkError) as error:
        return DeliveryResult(False, True, type(error).__name__)
    result = _response_result(response)
    if not result.sent and endpoint == "sendPhoto":
        try:
            fallback = client.post(
                f"{base}/sendMessage",
                json={"chat_id": job.recipient.chat_id, "text": text},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            return DeliveryResult(False, True, type(error).__name__)
        return _response_result(fallback)
    return result


class NotificationService:
    """Small durable outbox shared by one DeepStream worker."""

    def __init__(
        self,
        config: dict[str, Any],
        evidence_root: Path,
        run_id: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.evidence_root = evidence_root
        self.run_id = run_id
        self.project_root = Path(__file__).resolve().parents[3]
        raw = config.get("notifications", {}) or {}
        self.enabled = bool(raw.get("enabled", False))
        self.cooldown_seconds = max(0.0, float(raw.get("cooldown_seconds", 30)))
        retry = raw.get("retry", {}) or {}
        self.max_attempts = max(1, int(retry.get("max_attempts", 3)))
        self.initial_backoff = max(0.1, float(retry.get("initial_backoff_seconds", 2)))
        self.max_backoff = max(self.initial_backoff, float(retry.get("max_backoff_seconds", 60)))
        self.public_base_url = _resolve(
            raw.get("public_base_url", ""), _environment()
        )
        public_url_env = str(raw.get("public_base_url_env", "")).strip()
        if public_url_env:
            self.public_base_url = _environment().get(public_url_env, "").strip()
        self.providers = self._load_providers(raw)
        severity_config = raw.get("severity", {}) or {}
        self.default_severity = str(severity_config.get("default", "low"))
        self.severity_rules = list(severity_config.get("rules", []) or [])
        self.rules = list(raw.get("rules", []) or [])
        self._transport = transport
        self._db_lock = threading.RLock()
        self.db = sqlite3.connect(
            str(self.evidence_root / "notifications.sqlite3"),
            check_same_thread=False,
            timeout=30,
        )
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS notification_delivery ("
            "idempotency_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, "
            "function TEXT NOT NULL, camera TEXT NOT NULL, lifecycle TEXT NOT NULL, "
            "provider TEXT NOT NULL, recipient_id TEXT NOT NULL, status TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL, "
            "payload TEXT NOT NULL)"
        )
        self.db.commit()
        self._queue: queue.Queue[NotificationJob | None] = queue.Queue(
            maxsize=max(1, int(raw.get("queue_size", 256)))
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._run,
                name=f"notifications-{config.get('input', {}).get('camera', 'camera')}",
                daemon=True,
            )
            self._thread.start()
        LOG.info(
            "notifications enabled=%s telegram_enabled=%s zalo_enabled=%s public_media=%s",
            self.enabled,
            self.providers["telegram"].enabled and self.providers["telegram"].configured,
            self.providers["zalo"].enabled and self.providers["zalo"].configured,
            bool(self.public_base_url),
        )

    @staticmethod
    def _load_providers(raw: dict[str, Any]) -> dict[str, Provider]:
        environment = _environment()
        channels = raw.get("channels") or raw.get("providers") or {}
        result: dict[str, Provider] = {}
        for name in ("telegram", "zalo"):
            value = channels.get(name, {}) or {}
            recipients: list[Recipient] = []
            for item in value.get("recipients", []) or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                chat_id = _resolve(item.get("chat_id", ""), environment)
                chat_id_env = str(item.get("chat_id_env", "")).strip()
                if chat_id_env:
                    chat_id = environment.get(chat_id_env, "").strip()
                if chat_id:
                    recipients.append(Recipient(str(item["id"]), chat_id))
            token_env = str(value.get("token_env", f"{name.upper()}_BOT_TOKEN"))
            result[name] = Provider(
                name=name,
                enabled=bool(value.get("enabled", False)),
                token=environment.get(token_env, "").strip(),
                recipients=tuple(recipients),
            )
        return result

    def _snapshot_url(self, path: Path | None) -> str | None:
        if not self.public_base_url or path is None:
            return None
        try:
            relative = path.resolve().relative_to(self.project_root.resolve()).as_posix()
        except ValueError:
            return None
        return f"{self.public_base_url.rstrip('/')}/{quote(relative, safe='/')}"

    @staticmethod
    def _snapshot(path: Path) -> Path | None:
        for pattern in ("*-annotated.jpg", "*-full.jpg"):
            candidates = sorted(path.glob(f"snapshots/{pattern}"), key=lambda item: item.stat().st_mtime)
            if candidates:
                return candidates[-1]
        return None

    def _severity(self, event: dict[str, Any]) -> str:
        explicit = str(event.get("severity", "")).strip().lower()
        if explicit:
            return explicit
        function = str(event.get("function", ""))
        classification = str(event.get("classification", ""))
        for rule in self.severity_rules:
            functions = rule.get("functions", []) or []
            classifications = rule.get("classifications", []) or []
            if isinstance(functions, str):
                functions = [functions]
            if isinstance(classifications, str):
                classifications = [classifications]
            if functions and function not in functions:
                continue
            if classifications and classification not in classifications:
                continue
            return str(rule.get("severity", self.default_severity)).lower()
        return self.default_severity

    def _matches(self, rule: dict[str, Any], event: dict[str, Any]) -> bool:
        if not bool(rule.get("enabled", True)):
            return False
        function = str(event.get("function", ""))
        camera = str(event.get("camera_id", ""))
        functions = rule.get("functions", rule.get("function", []))
        if isinstance(functions, str):
            functions = [functions]
        cameras = rule.get("cameras", []) or []
        severities = rule.get("severities", rule.get("severity", [])) or []
        if isinstance(severities, str):
            severities = [severities]
        return (
            (not functions or function in functions)
            and (not cameras or camera in cameras)
            and (not severities or self._severity(event) in severities)
        )

    def _recipients(self, event: dict[str, Any]) -> list[tuple[Provider, Recipient]]:
        selected: set[tuple[str, str]] = set()
        for rule in self.rules:
            if not self._matches(rule, event):
                continue
            destinations = rule.get("destinations", {}) or {}
            for provider_name in ("telegram", "zalo"):
                provider = self.providers[provider_name]
                if not provider.enabled or not provider.configured:
                    continue
                ids = destinations.get(provider_name, []) or []
                for recipient in provider.recipients:
                    if recipient.id in ids:
                        selected.add((provider_name, recipient.id))
        return [
            (self.providers[name], recipient)
            for name, recipient_id in sorted(selected)
            for recipient in self.providers[name].recipients
            if recipient.id == recipient_id
        ]

    @staticmethod
    def _message(event: dict[str, Any], severity: str) -> tuple[str, str]:
        function = str(event.get("function", "event"))
        classification = str(event.get("classification", function))
        camera = str(event.get("camera_id", "camera"))
        score = float(event.get("last_score", 0) or 0)
        if function == "smoking_behavior":
            label = "Hút thuốc"
        elif function == "fire_smoke":
            label = "Lửa" if classification == "fire" else "Khói"
        elif function == "face_recognition":
            label = "Nhận diện khuôn mặt"
        else:
            label = "Cảnh báo camera"
        camera_label = camera[:1].upper() + camera[1:]
        return f"[{severity.upper()}] {label} - {camera_label} {score * 100:.0f}%", ""

    def notify_event(self, event_id: str, lifecycle: str, event_directory: Path | None) -> None:
        if not self.enabled or lifecycle != "START" or event_directory is None:
            return
        event_path = event_directory / "event.json"
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("notification event artifact unavailable: %s", event_id)
            return
        if not isinstance(event, dict):
            return
        severity = self._severity(event)
        title, message = self._message(event, severity)
        snapshot = self._snapshot(event_directory)
        snapshot_url = self._snapshot_url(snapshot)
        camera = str(event.get("camera_id", "camera"))
        function = str(event.get("function", "event"))
        for provider, recipient in self._recipients(event):
            with self._db_lock:
                cooldown_hit = self.cooldown_seconds and self.db.execute(
                    "SELECT 1 FROM notification_delivery WHERE provider=? AND "
                    "recipient_id=? AND camera=? AND function=? AND status='sent' "
                    "AND updated_at>? LIMIT 1",
                    (
                        provider.name,
                        recipient.id,
                        camera,
                        function,
                        time.time() - self.cooldown_seconds,
                    ),
                ).fetchone()
            if cooldown_hit:
                LOG.info(
                    "notification suppressed by cooldown provider=%s recipient=%s camera=%s",
                    provider.name,
                    recipient.id,
                    camera,
                )
                continue
            key = f"{self.run_id}|{event_id}|{lifecycle}|{provider.name}|{recipient.id}"
            now = time.time()
            payload = json.dumps(
                {
                    "event_id": event_id,
                    "severity": severity,
                    "title": title,
                    "message": message,
                    "snapshot_path": str(snapshot) if snapshot else None,
                    "snapshot_url": snapshot_url,
                },
                separators=(",", ":"),
            )
            try:
                with self._db_lock:
                    cursor = self.db.execute(
                        "INSERT OR IGNORE INTO notification_delivery("
                        "idempotency_key,event_id,function,camera,lifecycle,provider,"
                        "recipient_id,status,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            event_id,
                            function,
                            camera,
                            lifecycle,
                            provider.name,
                            recipient.id,
                            "pending",
                            now,
                            now,
                            payload,
                        ),
                    )
                    self.db.commit()
            except sqlite3.Error:
                LOG.exception("notification outbox insert failed: %s", key)
                continue
            if cursor.rowcount == 0:
                continue
            job = NotificationJob(
                key,
                event_id,
                function,
                camera,
                lifecycle,
                provider,
                recipient,
                title,
                message,
                snapshot,
                snapshot_url,
            )
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._mark(job, "failed", "notification queue is full")

    def _mark(self, job: NotificationJob, status: str, error: str | None = None) -> None:
        now = time.time()
        with self._db_lock:
            self.db.execute(
                "UPDATE notification_delivery SET status=?, "
                "last_error=?, updated_at=?, completed_at=? WHERE idempotency_key=?",
                (
                    status,
                    error,
                    now,
                    now if status in {"sent", "failed"} else None,
                    job.idempotency_key,
                ),
            )
            self.db.commit()

    def _run(self) -> None:
        with httpx.Client(timeout=20.0, transport=self._transport) as client:
            while not self._stop.is_set():
                try:
                    job = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if job is None:
                    break
                for attempt in range(1, self.max_attempts + 1):
                    with self._db_lock:
                        self.db.execute(
                            "UPDATE notification_delivery SET status='processing', "
                            "attempts=?, updated_at=? WHERE idempotency_key=?",
                            (attempt, time.time(), job.idempotency_key),
                        )
                        self.db.commit()
                    result = (
                        _deliver_telegram(client, job)
                        if job.provider.name == "telegram"
                        else _deliver_zalo(client, job)
                    )
                    if result.sent:
                        self._mark(job, "sent")
                        LOG.info(
                            "notification sent provider=%s recipient=%s event=%s",
                            job.provider.name,
                            job.recipient.id,
                            job.event_id,
                        )
                        break
                    if not result.retryable or attempt >= self.max_attempts:
                        self._mark(job, "failed", result.error)
                        LOG.warning(
                            "notification failed provider=%s recipient=%s event=%s error=%s",
                            job.provider.name,
                            job.recipient.id,
                            job.event_id,
                            result.error,
                        )
                        break
                    delay = min(self.max_backoff, self.initial_backoff * (2 ** (attempt - 1)))
                    self._stop.wait(delay)
                self._queue.task_done()

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"enabled": self.enabled}
        for name, provider in self.providers.items():
            with self._db_lock:
                rows = self.db.execute(
                    "SELECT status, completed_at, last_error FROM notification_delivery "
                    "WHERE provider=? ORDER BY updated_at DESC",
                    (name,),
                ).fetchall()
            sent = next((row for row in rows if row[0] == "sent"), None)
            failed = next((row for row in rows if row[0] == "failed"), None)
            result[name] = {
                "enabled": provider.enabled,
                "configured": provider.configured,
                "pending": sum(row[0] in {"pending", "processing"} for row in rows),
                "last_success": sent[1] if sent else None,
                "last_error": failed[2] if failed else None,
            }
        return result

    def close(self) -> None:
        if self._thread is not None:
            self._stop.set()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._thread.join(timeout=5)
        self.db.close()
