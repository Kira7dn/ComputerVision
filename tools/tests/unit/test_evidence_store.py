from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from deepstream_safety.events import SafetyDetection, SafetyEventStore
from deepstream_safety.evidence import EvidenceStore
from deepstream_safety.fire_smoke_engine import FireSmokeDetection
from deepstream_safety.fire_smoke_events import FireSmokeEventStore


def _config(root: Path) -> dict:
    return {
        "evidence": {
            "directory": str(root),
            "prefix": "snapshots-acceptance",
            "snapshot_interval_ms": 1000,
        },
        "input": {"camera": "camera_face", "rtsp_url": "rtsp://face"},
        "functions": {"trace": True, "face_recognition": True},
    }


def test_event_lifecycle_is_classified_and_deduplicated(tmp_path: Path) -> None:
    store = EvidenceStore(_config(tmp_path), "run-001")
    frame = np.full((24, 32, 3), 127, dtype=np.uint8)
    event_id = store.start_event(
        event_id="face-run-001-camera_face-7",
        function="face_recognition",
        classification="pending",
        camera_id="camera_face",
        person_track_id=7,
        pending=True,
        frame=frame,
        frame_number=10,
        bbox=(2, 3, 20, 22),
        score=0.91,
    )

    assert not store.record(
        event_id,
        "START",
        {"duplicate": True},
        frame=frame,
        frame_number=10,
        bbox=(2, 3, 20, 22),
        score=0.91,
    )
    store.finish_event(
        event_id,
        classification="recognized",
        identity="alice",
        payload={"event": "track_end"},
        frame=frame,
        frame_number=20,
        bbox=(2, 3, 20, 22),
        score=0.95,
    )
    store.close()

    event_dir = tmp_path / "snapshots-acceptance-run-001" / "camera_face" / "face_recognition" / event_id
    assert (event_dir / "event.json").is_file()
    event = json.loads((event_dir / "event.json").read_text(encoding="utf-8"))
    assert event["classification"] == "recognized"
    assert event["identity"] == "alice"
    trace_rows = [json.loads(line) for line in (event_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["record_type"] for row in trace_rows] == ["START", "END"]
    assert all(row["idempotency_key"].startswith("run-001|worker-0|") for row in trace_rows)
    assert len(list((event_dir / "snapshots").glob("*.jpg"))) == 6
    assert len(list((event_dir / "snapshots").glob("*-annotated.jpg"))) == 2

    index = sqlite3.connect(str(tmp_path / "snapshots-acceptance-run-001" / "index.sqlite3"))
    try:
        assert index.execute("SELECT COUNT(*) FROM records WHERE kind = 'trace'").fetchone()[0] == 2
    finally:
        index.close()
    assert len((tmp_path / "snapshots-acceptance-run-001" / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_smoking_lifecycle_uses_function_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["input"]["camera"] = "camera_safety"
    config["events"] = {
        "enabled": True,
        "camera": "camera_safety",
        "confirm_seconds": 0.0,
        "clear_seconds": 5.0,
        "trace_interval_ms": 400,
    }
    store = EvidenceStore(config, "run-002")
    events = SafetyEventStore(config, store)
    frame = np.full((24, 32, 3), 80, dtype=np.uint8)
    detection = SafetyDetection(11, 0.72, (3.0, 4.0, 22.0, 23.0), (1.0, 2.0, 24.0, 24.0))
    second_detection = SafetyDetection(12, 0.68, (5.0, 6.0, 24.0, 23.0), (3.0, 5.0, 26.0, 24.0))

    transition = events.observe(1, 1.0, [detection, second_detection], frame)
    assert transition is not None and transition.operation == "START"
    event_ids = set(events.active_event_ids)
    assert len(event_ids) == 2
    events.observe(2, 2.0, [], frame)
    ended = events.observe(3, 8.0, [], frame)
    assert ended is not None and ended.operation == "END"
    store.close()

    observed_bboxes: set[tuple[float, ...]] = set()
    for event_id in event_ids:
        event_dir = tmp_path / "snapshots-acceptance-run-002" / "camera_safety" / "smoking_behavior" / event_id
        event = json.loads((event_dir / "event.json").read_text(encoding="utf-8"))
        assert event["status"] == "ended"
        trace_rows = [json.loads(line) for line in (event_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [row["record_type"] for row in trace_rows] == ["START", "END"]
        observed_bboxes.add(tuple(trace_rows[0]["person_bbox"]))
        assert trace_rows[0]["model_roi_bbox"] is not None
    assert observed_bboxes == {(3.0, 4.0, 22.0, 23.0), (5.0, 6.0, 24.0, 23.0)}


def test_fire_smoke_has_independent_class_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["input"]["camera"] = "camera_safety"
    config["fire_smoke"] = {"enabled": True, "confirmation_hits": 2, "confirmation_window": 4, "clear_seconds": 3}
    store = EvidenceStore(config, "run-003")
    events = FireSmokeEventStore(config, store)
    frame = np.full((24, 32, 3), 50, dtype=np.uint8)
    detections = [
        FireSmokeDetection("fire", 0.42, (1.0, 2.0, 15.0, 18.0)),
        FireSmokeDetection("smoke", 0.31, (4.0, 5.0, 20.0, 22.0)),
    ]

    assert events.observe(frame_num=1, timestamp=1.0, detections=detections, frame=frame) == []
    transitions = events.observe(frame_num=2, timestamp=2.0, detections=detections, frame=frame)
    assert {transition.label for transition in transitions} == {"fire", "smoke"}
    assert len(events.active_event_ids) == 2
    events.observe(frame_num=3, timestamp=3.0, detections=[], frame=frame)
    events.observe(frame_num=4, timestamp=7.0, detections=[], frame=frame)
    store.close()

    event_dirs = list((tmp_path / "snapshots-acceptance-run-003" / "camera_safety" / "fire_smoke").glob("*"))
    assert {json.loads((path / "event.json").read_text(encoding="utf-8"))["classification"] for path in event_dirs} == {"fire", "smoke"}
    assert all(json.loads((path / "event.json").read_text(encoding="utf-8"))["status"] == "ended" for path in event_dirs)
