import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.models.dms_engine import (
    AlertSmoother,
    DmsDetection,
    NeutralPoseCalibrator,
    compute_face_metrics,
    crop_driver_face_input,
    select_dms_overlay_detections,
    select_primary_driver,
)
from adapters.persistence.evidence_repository import EvidenceStore
from domain.dms_events import DmsAlertEventStore

if not hasattr(os, "sysconf"):
    os.sysconf = lambda _name: 100  # type: ignore[attr-defined]

from interfaces import dashboard_api


def _config(tmp_path, epoch: str = "epoch-1") -> dict:
    return {
        "input": {"camera": "DMS"},
        "runtime": {"worker_epoch": epoch},
        "evidence": {"directory": str(tmp_path), "prefix": "dms"},
        "dms": {
            "enabled": True,
            "face_mesh": {
                "ear_threshold": 0.20,
                "mar_threshold": 0.65,
                "yaw_threshold_deg": 16.0,
                "pitch_threshold_deg": 14.0,
            },
            "event_policy": {
                "model": {
                    "min_score": 0.35,
                    "require_person_match": True,
                    "confirmation_hits": 3,
                    "confirmation_window": 4,
                    "minimum_duration_seconds": 0.4,
                    "candidate_timeout_seconds": 0.6,
                    "clear_seconds": 0.4,
                    "unknown_timeout_seconds": 0.4,
                    "trace_interval_ms": 100,
                },
                "face": {
                    "confirmation_hits": 2,
                    "confirmation_window": 3,
                    "minimum_duration_seconds": 0.2,
                    "candidate_timeout_seconds": 0.4,
                    "clear_seconds": 0.4,
                    "unknown_timeout_seconds": 0.2,
                    "trace_interval_ms": 100,
                },
                "no_seatbelt": {
                    "confirmation_hits": 3,
                    "confirmation_window": 4,
                    "minimum_duration_seconds": 0.4,
                    "candidate_timeout_seconds": 0.6,
                    "clear_seconds": 0.4,
                    "unknown_timeout_seconds": 0.4,
                    "trace_interval_ms": 100,
                },
            },
        },
    }


def _detection(
    label: str,
    score: float,
    *,
    track_id: int | None = 1,
) -> DmsDetection:
    return DmsDetection(
        source="test-model",
        original_class=label,
        label=label,
        score=score,
        bbox=(10.0, 10.0, 50.0, 80.0),
        person_track_id=track_id,
    )


def _result(
    *,
    detections: tuple[DmsDetection, ...] = (),
    metrics: dict | None = None,
    alerts: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        status="ALERT" if alerts else "OK",
        alerts=alerts,
        metrics={
            "object_models_available": True,
            "object_providers": {"test-model": ["CPUExecutionProvider"]},
            "driver_person_count": 1,
            "driver_person_track_ids": [1],
            "driver_person_bboxes": [[0.0, 0.0, 100.0, 100.0]],
            "face_detected": False,
            "ear": None,
            "mar": None,
            "pose_calibrated": False,
            "yaw_deg": None,
            "pitch_deg": None,
            **(metrics or {}),
        },
        detections=detections,
    )


def test_dms_alert_smoother_matches_reference_hysteresis() -> None:
    smoother = AlertSmoother(on_frames=3, off_frames=2)

    assert smoother.update(["Smoking"]) == []
    assert smoother.update(["Smoking"]) == []
    assert smoother.update(["Smoking"]) == ["Smoking"]
    assert smoother.update([]) == ["Smoking"]
    assert smoother.update([]) == ["Smoking"]
    assert smoother.update([]) == ["Smoking"]
    assert smoother.update([]) == ["Smoking"]
    assert smoother.update([]) == []


def test_dms_alert_smoother_ignores_non_alert_classes() -> None:
    smoother = AlertSmoother()
    assert smoother.update(["Safe Driving", "Seatbelt"]) == []
    assert smoother.update(["Safe Driving", "Seatbelt"]) == []
    assert smoother.update(["Safe Driving", "Seatbelt"]) == []


def test_dms_overlay_keeps_one_strongest_box_per_behavior_and_driver() -> None:
    strongest = DmsDetection(
        source="soham",
        original_class="Smoking",
        label="Smoking",
        score=0.69,
        bbox=(100.0, 100.0, 180.0, 180.0),
        person_track_id=7,
    )
    weaker_overlap = DmsDetection(
        source="soham",
        original_class="Smoking",
        label="Smoking",
        score=0.63,
        bbox=(104.0, 96.0, 176.0, 182.0),
        person_track_id=7,
    )

    selected = select_dms_overlay_detections((weaker_overlap, strongest))

    assert selected == [strongest]


def test_dms_overlay_keeps_same_behavior_for_different_drivers() -> None:
    first = _detection("Smoking", 0.70, track_id=1)
    second = _detection("Smoking", 0.65, track_id=2)

    assert select_dms_overlay_detections((first, second)) == [first, second]


def test_dms_face_metrics_use_reference_thresholds() -> None:
    points = [SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
    for index in range(478):
        points[index] = SimpleNamespace(
            x=0.2 + (index % 10) * 0.06,
            y=0.2 + (index % 20) * 0.03,
        )
    points[33] = SimpleNamespace(x=0.35, y=0.45)
    points[160] = SimpleNamespace(x=0.40, y=0.40)
    points[158] = SimpleNamespace(x=0.42, y=0.40)
    points[133] = SimpleNamespace(x=0.50, y=0.45)
    points[153] = SimpleNamespace(x=0.40, y=0.50)
    points[144] = SimpleNamespace(x=0.42, y=0.50)
    points[362] = SimpleNamespace(x=0.60, y=0.45)
    points[385] = SimpleNamespace(x=0.62, y=0.40)
    points[387] = SimpleNamespace(x=0.64, y=0.40)
    points[263] = SimpleNamespace(x=0.75, y=0.45)
    points[373] = SimpleNamespace(x=0.62, y=0.50)
    points[380] = SimpleNamespace(x=0.64, y=0.50)
    points[61] = SimpleNamespace(x=0.45, y=0.65)
    points[13] = SimpleNamespace(x=0.60, y=0.55)
    points[291] = SimpleNamespace(x=0.75, y=0.65)
    points[14] = SimpleNamespace(x=0.60, y=0.78)
    points[1] = SimpleNamespace(x=0.50, y=0.50)

    metrics = compute_face_metrics(points)

    assert metrics["face_detected"] is True
    assert metrics["ear"] == pytest.approx(0.667, abs=0.02)
    assert metrics["mar"] == pytest.approx(0.767, abs=0.02)
    assert metrics["raw_alerts"] == ["Yawning"]


def test_driver_face_crop_selects_largest_person_and_bounds_resolution() -> None:
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    selected = select_primary_driver(
        {
            7: (700.0, 150.0, 1550.0, 1050.0),
            8: (1700.0, 200.0, 1900.0, 500.0),
        }
    )

    assert selected is not None
    assert selected[0] == 7
    cropped = crop_driver_face_input(
        frame,
        selected[1],
        upper_body_ratio=0.55,
        padding_ratio=0.10,
        max_side=640,
    )
    assert cropped.size > 0
    assert max(cropped.shape[:2]) == 640


def test_neutral_pose_calibration_uses_camera_specific_straight_ahead() -> None:
    calibrator = NeutralPoseCalibrator(
        {
            "minimum_samples": 5,
            "window_size": 5,
            "max_yaw_std_deg": 2.0,
            "max_pitch_std_deg": 2.0,
            "neutral_update_alpha": 0.0,
        }
    )

    for yaw, pitch in (
        (23.8, -2.1),
        (24.2, -1.9),
        (24.0, -2.0),
        (23.9, -2.2),
    ):
        assert calibrator.update(yaw, pitch)["pose_calibrated"] is False
    straight = calibrator.update(24.1, -1.8)

    assert straight["pose_calibrated"] is True
    assert straight["neutral_yaw_deg"] == pytest.approx(24.0, abs=0.2)
    assert straight["neutral_pitch_deg"] == pytest.approx(-2.0, abs=0.2)
    assert abs(straight["yaw_deg"]) < 0.3
    assert abs(straight["pitch_deg"]) < 0.3

    turned = calibrator.update(43.0, -2.0)
    assert turned["yaw_deg"] == pytest.approx(19.0, abs=0.2)
    assert abs(turned["pitch_deg"]) < 0.2


def test_low_score_smoking_never_becomes_an_event(tmp_path) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "run-low-score")
    store = DmsAlertEventStore(config, evidence)
    seatbelt = _detection("Seatbelt", 0.8)
    low_smoking = _detection("Smoking", 0.34)

    for index in range(8):
        transitions = store.observe(
            frame_num=index,
            timestamp=index * 0.2,
            result=_result(detections=(seatbelt, low_smoking)),
            frame=None,
        )
        assert transitions == []

    assert store.active_event_ids == []
    assert store.candidate_labels == []
    evidence.close()


def test_confirmed_smoking_has_one_lifecycle_and_complete_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "run-smoking")
    store = DmsAlertEventStore(config, evidence)
    frame = np.full((24, 32, 3), 80, dtype=np.uint8)
    smoking = _detection("Smoking", 0.62)
    seatbelt = _detection("Seatbelt", 0.82)
    positive = _result(detections=(smoking, seatbelt), alerts=("Smoking",))

    assert store.observe(frame_num=1, timestamp=1.0, result=positive, frame=frame) == []
    assert store.observe(frame_num=2, timestamp=1.2, result=positive, frame=frame) == []
    started = store.observe(
        frame_num=3,
        timestamp=1.4,
        result=positive,
        frame=frame,
    )
    assert [item.operation for item in started] == ["START"]
    event_id = started[0].event_id

    updated = store.observe(
        frame_num=4,
        timestamp=1.6,
        result=positive,
        frame=frame,
    )
    assert [item.operation for item in updated] == ["UPDATE"]

    negative = _result(detections=(seatbelt,))
    assert store.observe(frame_num=5, timestamp=1.8, result=negative, frame=frame) == []
    assert store.observe(frame_num=6, timestamp=2.0, result=negative, frame=frame) == []
    ended = store.observe(
        frame_num=7,
        timestamp=2.2,
        result=negative,
        frame=frame,
    )
    assert [item.operation for item in ended] == ["END"]
    assert ended[0].event_id == event_id

    event_dir = evidence.event_directory(event_id)
    records = [
        json.loads(line)
        for line in (event_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["record_type"] for item in records] == ["START", "UPDATE", "END"]
    assert all(item["dms_alerts"] == ["Smoking"] for item in records)
    assert records[0]["dms_evidence"]["type"] == "model_detection"
    assert records[0]["dms_evidence"]["confirmation_hits"] == 3
    assert records[0]["dms_evidence"]["required_hits"] == 3
    assert records[-1]["confirmation_state"] == "CLOSED"
    assert records[-1]["end_reason"] == "confirmed_clear"
    assert records[-1]["dms_evidence"]["best_score"] == pytest.approx(0.62)

    monkeypatch.setattr(dashboard_api, "load_raw_config", lambda _path: config)
    feed = dashboard_api._event_feed(0, 50)
    matching = [item for item in feed["events"] if item["event_id"] == event_id]
    assert len(matching) == 1
    assert matching[0]["state"] == "ended"
    assert matching[0]["details"]["dms_evidence"]["type"] == "model_detection"
    evidence.close()


def test_driver_inattention_has_one_event_for_changing_reasons(tmp_path) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "run-attention")
    store = DmsAlertEventStore(config, evidence)
    seatbelt = _detection("Seatbelt", 0.8)

    distracted = _result(
        detections=(seatbelt,),
        metrics={
            "driver_attention": {
                "event_active": True,
                "state": "distracted",
                "score": 34,
                "alert_level": "critical",
                "reasons": ["pose"],
                "source": "current",
            }
        }
    )
    assert store.observe(frame_num=10, timestamp=1.0, result=distracted, frame=None) == []
    started = store.observe(frame_num=11, timestamp=1.1, result=distracted, frame=None)
    assert [item.operation for item in started] == ["START"]
    assert started[0].label == "Driver Inattention"

    phone = _result(
        detections=(seatbelt,),
        metrics={
            "driver_attention": {
                "event_active": True,
                "state": "distracted",
                "score": 12,
                "alert_level": "emergency",
                "reasons": ["phone"],
                "source": "openpilot",
            }
        }
    )
    updates = store.observe(frame_num=12, timestamp=1.3, result=phone, frame=None)
    assert all(item.label == "Driver Inattention" for item in updates)
    assert len(store.active_event_ids) == 1

    attentive = _result(
        detections=(seatbelt,),
        metrics={
            "driver_attention": {
                "event_active": False,
                "state": "attentive",
                "score": 80,
                "alert_level": "none",
                "reasons": [],
                "source": "openpilot",
            }
        }
    )
    ended = store.observe(frame_num=13, timestamp=1.4, result=attentive, frame=None)
    assert [item.operation for item in ended] == ["END"]
    evidence.close()


def test_no_seatbelt_requires_a_tracked_driver(tmp_path) -> None:
    config = _config(tmp_path)
    evidence = EvidenceStore(config, "run-seatbelt")
    store = DmsAlertEventStore(config, evidence)

    no_driver = _result(
        metrics={
            "driver_person_count": 0,
            "driver_person_track_ids": [],
            "driver_person_bboxes": [],
        }
    )
    for index in range(5):
        assert store.observe(
            frame_num=index,
            timestamp=index * 0.2,
            result=no_driver,
            frame=None,
        ) == []
    assert "No Seatbelt" not in store.candidate_labels

    driver_without_seatbelt = _result()
    assert store.observe(
        frame_num=10,
        timestamp=1.0,
        result=driver_without_seatbelt,
        frame=None,
    ) == []
    assert store.observe(
        frame_num=11,
        timestamp=1.2,
        result=driver_without_seatbelt,
        frame=None,
    ) == []
    started = store.observe(
        frame_num=12,
        timestamp=1.4,
        result=driver_without_seatbelt,
        frame=None,
    )
    assert [item.operation for item in started] == ["START"]
    assert started[0].label == "No Seatbelt"

    seatbelt = _result(detections=(_detection("Seatbelt", 0.8),))
    assert store.observe(frame_num=13, timestamp=1.6, result=seatbelt, frame=None) == []
    assert store.observe(frame_num=14, timestamp=1.8, result=seatbelt, frame=None) == []
    ended = store.observe(
        frame_num=15,
        timestamp=2.0,
        result=seatbelt,
        frame=None,
    )
    assert [item.operation for item in ended] == ["END"]
    evidence.close()
