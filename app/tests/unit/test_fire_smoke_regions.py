from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ls_vision.adapters.persistence.evidence_repository import EvidenceStore
from ls_vision.domain.detections import FireSmokeDetection
from ls_vision.domain.fire_smoke_events import (
    DynamicsResult,
    FireSmokeEventStore,
    RegionDynamicsVerifier,
)


def _config(tmp_path: Path, *, hits: int = 4, window: int = 6) -> dict:
    return {
        "input": {"camera": "DMS"},
        "runtime": {"worker_epoch": "epoch-test"},
        "evidence": {"directory": str(tmp_path), "prefix": "regions"},
        "fire_smoke": {
            "enabled": True,
            "tracking": {
                "confirmation_hits": hits,
                "confirmation_window": window,
                "minimum_duration_seconds": 1.5,
                "notification_min_duration_seconds": 3.0,
                "clear_seconds": 3.0,
                "match_iou": 0.10,
                "match_center_distance": 0.20,
                "min_area_ratio": 0.25,
                "max_area_ratio": 4.0,
                "bbox_smoothing_alpha": 0.35,
            },
            "dynamics": {
                "mode": "advisory",
                "confirmation_votes": 3,
                "confirmation_window": 5,
            },
        },
    }


def _detection(left: float, *, score: float = 0.6, label: str = "fire", size: float = 20) -> FireSmokeDetection:
    return FireSmokeDetection(label, score, (left, 10.0, left + size, 10.0 + size))


def _frame(value: int = 20) -> np.ndarray:
    return np.full((120, 200, 3), value, dtype=np.uint8)


def _observe(store: FireSmokeEventStore, number: int, timestamp: float, detections: list[FireSmokeDetection], frame: np.ndarray | None = None):
    return store.observe(
        frame_num=number,
        timestamp=timestamp,
        detections=detections,
        frame=_frame() if frame is None else frame,
    )


def test_region_matching_handles_motion_and_area_growth(tmp_path: Path) -> None:
    evidence = EvidenceStore(_config(tmp_path), "motion")
    events = FireSmokeEventStore(_config(tmp_path), evidence)

    _observe(events, 1, 0.0, [_detection(10, size=20)])
    _observe(events, 2, 0.5, [_detection(15, size=35)])

    assert events.metrics()["new_count"] == 1
    assert events.metrics()["matched_count"] == 1
    evidence.close()


def test_two_regions_do_not_merge_and_ids_are_deterministic(tmp_path: Path) -> None:
    config = _config(tmp_path, hits=2, window=2)
    config["fire_smoke"]["tracking"]["minimum_duration_seconds"] = 0.5
    evidence = EvidenceStore(config, "multi")
    events = FireSmokeEventStore(config, evidence)
    detections = [_detection(10), _detection(150)]

    _observe(events, 1, 0.0, detections)
    transitions = _observe(events, 2, 0.5, detections)

    starts = [item for item in transitions if item.operation == "START"]
    assert [item.region_track_id for item in starts] == [1, 2]
    assert [item.event_id for item in starts] == [
        "fire-epoch-test-region-000001",
        "fire-epoch-test-region-000002",
    ]
    assert len(events.visible_detections) == 2
    evidence.close()


def test_detector_m_of_n_and_minimum_duration_gate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "m-of-n")
    events = FireSmokeEventStore(config, evidence)

    assert not _observe(events, 1, 0.0, [_detection(10)])
    assert not _observe(events, 2, 0.4, [])
    assert not _observe(events, 3, 0.8, [_detection(11)])
    assert not _observe(events, 4, 1.2, [_detection(12)])
    transitions = _observe(events, 5, 1.6, [_detection(13)])

    assert [item.operation for item in transitions] == ["START"]
    assert events.metrics()["confirmation_latency_seconds"] == 1.6
    evidence.close()


def test_dynamics_votes_are_advisory_and_do_not_delay_confirmation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "dynamic")
    events = FireSmokeEventStore(config, evidence)
    events.dynamics.compare = lambda _previous, _current: DynamicsResult(True, conditions_met=2)  # type: ignore[method-assign]

    transitions = []
    for number, timestamp in enumerate((0.0, 0.5, 1.0, 1.5), start=1):
        transitions.extend(_observe(events, number, timestamp, [_detection(10 + number)]))

    assert [item.operation for item in transitions] == ["START"]
    assert events.metrics()["confirmed_tracks"] == 1
    evidence.close()


def test_static_candidate_is_confirmed_while_dynamics_remains_advisory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "static")
    events = FireSmokeEventStore(config, evidence)

    transitions = []
    for number, timestamp in enumerate((0.0, 0.5, 1.0, 1.5), start=1):
        transitions.extend(_observe(events, number, timestamp, [_detection(10)]))
    assert [item.operation for item in transitions] == ["START"]
    assert len(events.visible_detections) == 1
    assert events.metrics()["dynamics_mode"] == "advisory"
    assert events.metrics()["notification_count"] == 0
    evidence.close()


def test_notification_is_delayed_and_emitted_once(tmp_path: Path) -> None:
    config = _config(tmp_path, hits=2, window=2)
    config["fire_smoke"]["tracking"]["minimum_duration_seconds"] = 0.5
    config["fire_smoke"]["tracking"]["notification_min_duration_seconds"] = 1.5
    evidence = EvidenceStore(config, "notify")
    events = FireSmokeEventStore(config, evidence)

    _observe(events, 1, 0.0, [_detection(10)])
    assert [item.operation for item in _observe(events, 2, 0.5, [_detection(10)])] == ["START"]
    assert not [item for item in _observe(events, 3, 1.0, [_detection(10)]) if item.operation == "NOTIFY"]
    transitions = _observe(events, 4, 1.5, [_detection(10)])
    assert [item.operation for item in transitions if item.operation == "NOTIFY"] == ["NOTIFY"]
    assert not [item for item in _observe(events, 5, 2.0, [_detection(10)]) if item.operation == "NOTIFY"]
    assert events.metrics()["notification_count"] == 1
    assert events.metrics()["notification_latency_seconds"] == 1.5
    event_file = evidence.event_directory(events.active_event_ids[0]) / "event.json"  # type: ignore[operator]
    event = json.loads(event_file.read_text(encoding="utf-8"))
    assert event["notification_emitted"] is True
    trace = (event_file.parent / "trace.jsonl").read_text(encoding="utf-8")
    assert trace.count('"record_type":"NOTIFY"') == 1
    evidence.close()


def test_clearing_reacquire_keeps_one_event_then_ends(tmp_path: Path) -> None:
    config = _config(tmp_path, hits=2, window=2)
    config["fire_smoke"]["tracking"]["minimum_duration_seconds"] = 0.5
    evidence = EvidenceStore(config, "reacquire")
    events = FireSmokeEventStore(config, evidence)

    _observe(events, 1, 0.0, [_detection(10)])
    starts = _observe(events, 2, 0.5, [_detection(11)])
    event_id = starts[0].event_id
    assert not _observe(events, 3, 1.0, [])
    assert events.visible_detections[0].confirmation_state == "CLEARING"
    reacquired = _observe(events, 4, 2.0, [_detection(12)])
    assert not [item for item in reacquired if item.operation == "START"]
    assert events.active_event_ids == [event_id]
    _observe(events, 5, 2.5, [])
    ended = _observe(events, 6, 5.1, [])
    assert [item.operation for item in ended] == ["END"]
    assert ended[0].frame_num == 4
    assert ended[0].score == 0.6
    evidence.close()


def test_start_uses_best_frame_bbox_and_metadata(tmp_path: Path) -> None:
    config = _config(tmp_path, hits=2, window=2)
    config["fire_smoke"]["tracking"]["minimum_duration_seconds"] = 0.5
    evidence = EvidenceStore(config, "best")
    events = FireSmokeEventStore(config, evidence)
    first = _detection(10, score=0.9)
    second = _detection(12, score=0.6)

    _observe(events, 10, 0.0, [first], _frame(10))
    transition = _observe(events, 11, 0.5, [second], _frame(80))[0]

    assert transition.frame_num == 10
    assert transition.bbox == first.bbox
    event_file = evidence.event_directory(transition.event_id) / "event.json"  # type: ignore[operator]
    event = json.loads(event_file.read_text(encoding="utf-8"))
    assert event["region_track_id"] == 1
    assert event["confirmation_state"] == "CONFIRMED"
    assert event["detector_hits"] == 2
    assert event["best_bbox"] == list(first.bbox)
    assert event["best_frame_number"] == 10
    evidence.close()


def test_global_exposure_change_is_subtracted_from_dynamics() -> None:
    verifier = RegionDynamicsVerifier({})
    bbox = (40.0, 30.0, 120.0, 100.0)
    previous = verifier.sample(_frame(30), bbox)
    current = verifier.sample(_frame(90), bbox)

    result = verifier.compare(previous, current)

    assert result.changed_pixel_ratio == 0.0
    assert not result.dynamic


def test_shutdown_closes_each_confirmed_region(tmp_path: Path) -> None:
    config = _config(tmp_path, hits=2, window=2)
    config["fire_smoke"]["tracking"]["minimum_duration_seconds"] = 0.5
    evidence = EvidenceStore(config, "shutdown")
    events = FireSmokeEventStore(config, evidence)
    _observe(events, 1, 0.0, [_detection(10), _detection(150)])
    starts = _observe(events, 2, 0.5, [_detection(10), _detection(150)])

    events.close()

    assert len(starts) == 2
    for transition in starts:
        event_file = evidence.event_directory(transition.event_id) / "event.json"  # type: ignore[operator]
        event = json.loads(event_file.read_text(encoding="utf-8"))
        assert event["status"] == "ended"
        assert event["confirmation_state"] == "CLOSED"
    evidence.close()
