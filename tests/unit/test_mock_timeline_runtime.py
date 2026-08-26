from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import pytest

from application.mock_timeline_runtime import (
    ManagedPublisher,
    TimelineCamera,
    _aggregate_status,
    discover_timeline_cameras,
)
from bootstrap.config import load_raw_config

ROOT = Path(__file__).parents[2]


def test_discovers_four_synchronized_packet_publishers() -> None:
    cameras = discover_timeline_cameras(load_raw_config(ROOT / "config" / "dev.yaml"))

    assert [camera.camera_id for camera in cameras] == [
        "camera_front",
        "camera_back",
        "camera_left",
        "camera_right",
    ]
    assert [camera.camera_id for camera in cameras if not camera.media_only] == ["camera_front"]
    assert {camera.group for camera in cameras} == {"vehicle_surround"}
    assert {camera.period_seconds for camera in cameras} == {191.1}
    assert {camera.epoch_seconds for camera in cameras} == {0.0}
    assert {camera.publisher_mode for camera in cameras} == {"packet_copy"}
    assert {camera.camera_id: camera.fps for camera in cameras} == {
        "camera_front": 20,
        "camera_back": 10,
        "camera_left": 10,
        "camera_right": 10,
    }
    front = cameras[0]
    assert front.mock_video.name == "CAM_FRONT_20FPS_ALL_I.mp4"


def test_rejects_timeline_contract_drift() -> None:
    raw = deepcopy(load_raw_config(ROOT / "config" / "dev.yaml"))
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_left")
    camera["source"]["sync_period_seconds"] = 190.0

    with pytest.raises(ValueError, match="timeline differs"):
        discover_timeline_cameras(raw)


class ReadyPublisher:
    def status(self, _now: float) -> dict[str, object]:
        return {
            "mode": "publisher",
            "ready": True,
            "normalized_phase": 0.5,
        }


def test_aggregate_requires_all_publishers_ready(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.mp4"
    fixture.write_bytes(b"fixture")
    cameras = [
        TimelineCamera("front", "surround", 10.0, 0.0, False, fixture, "rtsp://x/front", 20),
        TimelineCamera("back", "surround", 10.0, 0.0, True, fixture, "rtsp://x/back", 15),
    ]

    publishers = {"front": ReadyPublisher(), "back": ReadyPublisher()}
    status = _aggregate_status(cameras, publishers)  # type: ignore[arg-type]

    assert status["ready"] is True
    assert status["groups"]["surround"]["locked"] is True
    assert status["groups"]["surround"]["cameras"]["back"]["mode"] == "publisher"

    publishers["back"] = type(
        "OfflinePublisher",
        (),
        {"status": lambda self, _now: {"mode": "publisher", "ready": False}},
    )()
    status = _aggregate_status(cameras, publishers)  # type: ignore[arg-type]
    assert status["ready"] is False


def test_publisher_status_contract_is_json_serializable(tmp_path: Path) -> None:
    fixture = tmp_path / "front.mp4"
    fixture.write_bytes(b"fixture")
    cameras = [
        TimelineCamera("front", "surround", 10.0, 0.0, False, fixture, "rtsp://x/front", 20)
    ]

    payload = _aggregate_status(cameras, {"front": ReadyPublisher()})  # type: ignore[arg-type]

    assert json.loads(json.dumps(payload))["schema_version"] == 1


def test_empty_timeline_is_ready_noop() -> None:
    payload = _aggregate_status([], {})

    assert payload["ready"] is True
    assert payload["groups"] == {}


def test_publisher_adopts_fresh_matching_process(tmp_path: Path, monkeypatch) -> None:
    fixture = tmp_path / "front.mp4"
    fixture.write_bytes(b"fixture")
    camera = TimelineCamera(
        "front", "surround", 10.0, 0.0, False, fixture, "rtsp://x/front", 20
    )
    status_path = tmp_path / "front.json"
    status_path.write_text(
        json.dumps(
            {
                "camera_id": "front",
                "sync_group": "surround",
                "publisher_mode": "frame_encode",
                "pid": 321,
                "ready": True,
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    publisher = ManagedPublisher(camera, status_path)
    monkeypatch.setattr(publisher, "_pid_alive", lambda pid: pid == 321)
    monkeypatch.setattr(
        "application.mock_timeline_runtime.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("publisher should be adopted"),
    )
    monkeypatch.setattr(
        "application.mock_timeline_runtime.wait_for_rtsp_video",
        lambda *args, **kwargs: None,
    )

    publisher.start()
    publisher.maintain(0.0)

    assert publisher.process is None
    assert publisher.adopted_pid == 321
    assert publisher.stream_verified is True


def test_publisher_does_not_adopt_a_different_mode(tmp_path: Path, monkeypatch) -> None:
    fixture = tmp_path / "front.mp4"
    fixture.write_bytes(b"fixture")
    camera = TimelineCamera(
        "front",
        "surround",
        10.0,
        0.0,
        False,
        fixture,
        "rtsp://x/front",
        20,
        "packet_copy",
    )
    status_path = tmp_path / "front.json"
    status_path.write_text(
        json.dumps(
            {
                "camera_id": "front",
                "sync_group": "surround",
                "publisher_mode": "frame_encode",
                "pid": 321,
                "ready": True,
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    publisher = ManagedPublisher(camera, status_path)
    monkeypatch.setattr(publisher, "_pid_alive", lambda pid: pid == 321)
    process = type("Process", (), {"pid": 654})()
    monkeypatch.setattr(
        "application.mock_timeline_runtime.subprocess.Popen",
        lambda command: (
            process
            if any("gstreamer_packet_publisher" in part for part in command)
            else None
        ),
    )

    publisher.start()

    assert publisher.process is process
    assert publisher.adopted_pid is None
