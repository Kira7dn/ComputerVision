from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import numpy as np

from adapters.notification.service import NotificationService
from adapters.persistence.evidence_repository import EvidenceStore


def _config(root: Path) -> dict:
    return {
        "input": {"camera": "camera_safety", "rtsp_url": "rtsp://safety"},
        "evidence": {"directory": str(root), "prefix": "run"},
        "notifications": {
            "enabled": True,
            "cooldown_seconds": 0,
            "channels": {
                "telegram": {
                    "enabled": True,
                    "recipients": [{"id": "ops", "chat_id": "telegram-chat"}],
                },
                "zalo": {
                    "enabled": True,
                    "recipients": [{"id": "ops", "chat_id": "zalo-chat"}],
                },
            },
            "rules": [
                {
                    "id": "safety",
                    "functions": ["smoking_behavior"],
                    "destinations": {"telegram": ["ops"], "zalo": ["ops"]},
                }
            ],
        },
    }


def test_notification_outbox_sends_telegram_and_zalo_without_blocking(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("ZALO_BOT_TOKEN", "zalo-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    config = _config(tmp_path)
    evidence = EvidenceStore(config, "run-001")
    event_id = evidence.start_event(
        event_id="smoking-event-1",
        function="smoking_behavior",
        classification="smoking",
        camera_id="camera_safety",
        frame=np.full((24, 32, 3), 80, dtype=np.uint8),
        frame_number=1,
        bbox=(2, 3, 20, 22),
        score=0.91,
    )
    service = NotificationService(
        config,
        evidence.root,
        "run-001",
        transport=httpx.MockTransport(handler),
    )
    try:
        service.notify_event(event_id, "START", evidence.event_directory(event_id))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            statuses = service.status()
            if statuses["telegram"]["last_success"] and statuses["zalo"]["last_success"]:
                break
            time.sleep(0.02)
        assert len(requests) == 2
        urls = {str(request.url) for request in requests}
        assert any("telegram-token/sendPhoto" in url for url in urls)
        assert any("zalo-token/sendMessage" in url for url in urls)

        service.notify_event(event_id, "START", evidence.event_directory(event_id))
        time.sleep(0.05)
        assert len(requests) == 2
        service.notify_event(event_id, "END", evidence.event_directory(event_id))
        time.sleep(0.05)
        assert len(requests) == 2
        rows = json.loads(
            json.dumps(
                service.db.execute(
                    "SELECT provider,status,attempts FROM notification_delivery "
                    "ORDER BY provider"
                ).fetchall()
            )
        )
        assert rows == [["telegram", "sent", 1], ["zalo", "sent", 1]]
    finally:
        service.close()
        evidence.close()


def test_notification_header_is_compact_and_contains_no_details(tmp_path: Path) -> None:
    title, message = NotificationService._message(
        {
            "function": "smoking_behavior",
            "classification": "smoking",
            "camera_id": "camera_safety",
            "last_score": 0.65,
            "identity": "ignored",
        },
        "high",
    )

    assert title == "[HIGH] Hút thuốc - Camera_safety 65%"
    assert message == ""
