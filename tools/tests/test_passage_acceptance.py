import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.passage_metrics import match_by_time_and_bbox, normalize_plate, score_face_passages
from tools.prepare_passage_fixture import load_manifest
import tools.validate_passage_acceptance as passage_validator
from tools.validate_passage_acceptance import api_sqlite_consistency, assign_records, correlation_mismatches, face_results, false_passage_count, lpr_results, update_anchor_state

MANIFEST = ROOT / "tools/fixtures/platform_passage_ground_truth.yaml"


def manifest_value() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def write_manifest(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "passage.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("current", "control", "expected"),
    [
        (
            {"face_camera": 2.9, "car_camera": 3.5},
            {"face_camera": 3.3, "car_camera": 3.9},
            True,
        ),
        (
            {"face_camera": 3.4, "car_camera": 4.0},
            {"face_camera": 3.3, "car_camera": 3.9},
            True,
        ),
        (
            {"face_camera": 3.41, "car_camera": 3.5},
            {"face_camera": 3.3, "car_camera": 3.9},
            False,
        ),
        (
            {"face_camera": 2.9, "car_camera": 3.5},
            {"face_camera": 3.3},
            False,
        ),
    ],
)
def test_skipped_fps_gate_uses_control_regression(
    current: dict[str, float], control: dict[str, float], expected: bool
) -> None:
    assert passage_validator.skipped_fps_within_control(current, control) is expected


def test_passage_manifest_contract() -> None:
    value = load_manifest(MANIFEST, ROOT)
    face = [p for p in value["face"]["passages"] if p.get("valid_passage", True)]
    lpr = [p for p in value["lpr"]["passages"] if p.get("valid_passage", True)]
    assert sum(p["expected_identity"] == "Jack" for p in face) >= 2
    assert sum(p["expected_identity"] == "unknown" for p in face) >= 2
    assert value["face"]["close_follow"]
    assert len(lpr) == 5
    assert sum(p["readable"] for p in lpr) == 3
    assert all(p["bbox"] and p["roi"] for p in lpr)


def test_passage_rejects_duplicate_id(tmp_path: Path) -> None:
    value = manifest_value(); value["lpr"]["passages"][0]["id"] = value["face"]["passages"][0]["id"]
    with pytest.raises(ValueError, match="globally unique"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_bad_window(tmp_path: Path) -> None:
    value = manifest_value(); value["face"]["passages"][0]["end_s"] = value["face"]["passages"][0]["start_s"]
    with pytest.raises(ValueError, match="invalid face passage window"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_bbox_outside_frame(tmp_path: Path) -> None:
    value = manifest_value(); value["face"]["passages"][0]["bbox"] = [0, 0, 1281, 100]
    with pytest.raises(ValueError, match="outside"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_readable_plate_without_label(tmp_path: Path) -> None:
    value = manifest_value(); value["lpr"]["passages"][0]["expected_plate"] = None
    with pytest.raises(ValueError, match="requires uppercase alphanumeric"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_anchor_handles_startup_delay_and_three_loops() -> None:
    state = {"mode": "black", "black_count": 0, "content_count": 0, "anchors": [], "means": []}
    samples = [
        60, 58, 16, 16, 16, 16, 16, 16, 16, 60, 61,
        60, 16, 16, 16, 16, 16, 16, 16, 62, 63,
        60, 16, 16, 16, 16, 16, 16, 16, 64, 65,
    ]
    for index, mean in enumerate(samples):
        update_anchor_state(state, mean, black_max=28, content_min=45, observed_at=100 + index * 0.2)
    assert len(state["anchors"]) == 3
    assert state["anchors"] == sorted(state["anchors"])


def test_anchor_ignores_internal_black_gap() -> None:
    state = {"mode": "await_black", "black_count": 0, "content_count": 0, "anchors": [], "means": []}
    for index, mean in enumerate([16, 16, 16, 16, 16, 60, 61]):
        update_anchor_state(state, mean, black_max=28, content_min=45, observed_at=100 + index * 0.2)
    assert state["anchors"] == []


def test_time_and_bbox_distinguish_simultaneous_vehicles() -> None:
    passages = [
        {"id": "a", "start_s": 1, "end_s": 2, "bbox": [0, 0, 100, 100]},
        {"id": "b", "start_s": 1, "end_s": 2, "bbox": [500, 0, 600, 100]},
    ]
    matched = match_by_time_and_bbox([{"frame_time": 1.5, "bbox": [505, 5, 595, 95]}], passages)
    assert list(matched) == ["b"]

    records = [{"camera": "car_camera", "frame_time": 100.5, "stage": "event_published", "object_box": [505, 5, 595, 95], "track_id": "t"}]
    assigned = assign_records(records, {"car_camera": [100]}, {"car_camera": 3}, {"car_camera": passages})
    assert assigned[0]["passage_id"] == "b"


def test_unknown_and_known_false_negative_are_scored() -> None:
    passages = [
        {"id": "known", "start_s": 0, "end_s": 2, "bbox": [0, 0, 100, 100], "expected_identity": "Jack"},
        {"id": "unknown", "start_s": 0, "end_s": 2, "bbox": [500, 0, 600, 100], "expected_identity": "unknown"},
    ]
    result = score_face_passages(
        [{"frame_time": 1, "bbox": [0, 0, 100, 100], "identity": "unknown"},
         {"frame_time": 1, "bbox": [500, 0, 600, 100], "identity": "Jack"}], passages,
    )
    assert result["recall"] == 0
    assert result["precision"] == 0


def test_close_follow_rejects_old_generation_correlation() -> None:
    records = [
        {"camera": "face_camera", "round_id": 1, "track_id": "t", "generation": 1, "passage_id": "known"},
        {"camera": "face_camera", "round_id": 1, "track_id": "t", "generation": 1, "passage_id": "unknown"},
    ]
    assert correlation_mismatches(records)
    records[1]["generation"] = 2
    assert not correlation_mismatches(records)


def test_face_passage_uses_majority_of_replay_rounds() -> None:
    passage = {
        "id": "known",
        "valid_passage": True,
        "start_s": 1.0,
        "expected_identity": "Jack",
    }
    records = []
    for round_id, anchor in ((1, 100.0), (2, 110.0)):
        records.extend(
            [
                {"passage_id": "known", "round_id": round_id, "stage": "first_qualified_face", "trace_time": anchor + 0.1, "frame_time": anchor + 0.1},
                {"passage_id": "known", "round_id": round_id, "stage": "first_attempt", "identity": "Jack", "trace_time": anchor + 0.2, "frame_time": anchor + 0.2},
                {"passage_id": "known", "round_id": round_id, "stage": "confirmed_result", "identity": "Jack", "trace_time": anchor + 0.3, "frame_time": anchor + 0.3},
            ]
        )
    result, _, *_ = face_results(records, [passage], [100.0, 110.0, 120.0])
    assert result["passages"][0]["detected_rounds"] == 2
    assert result["passages"][0]["correct_rounds"] == 2
    assert result["detection_recall"] == 1
    assert result["recall"] == 1


def test_false_passage_count_deduplicates_stage_updates() -> None:
    records = [
        {"camera": "face_camera", "round_id": 1, "track_id": "t", "generation": 1, "stage": "first_attempt"},
        {"camera": "face_camera", "round_id": 1, "track_id": "t", "generation": 1, "stage": "confirmed_result"},
    ]
    assert false_passage_count(records) == 1


def test_unreadable_counts_recall_not_exact_denominator() -> None:
    passages = [
        {"id": "readable", "valid_passage": True, "readable": True, "expected_plate": "ABC123", "accepted_plates": []},
        {"id": "unreadable", "valid_passage": True, "readable": False, "expected_plate": None, "accepted_plates": []},
    ]
    records = []
    for round_id in range(1, 4):
        records += [
            {"stage": "event_published", "passage_id": "readable", "round_id": round_id, "plate": "ABC123", "score": 0.95},
            {"stage": "event_published", "passage_id": "unreadable", "round_id": round_id, "plate": "NOISY", "score": 0.95},
        ]
        for passage_id in ("readable", "unreadable"):
            records += [{"stage": stage, "passage_id": passage_id, "round_id": round_id} for stage in ("detector_hit", "track_seen", "lpr_eligible", "plate_detected", "ocr_result")]
    result, false = lpr_results(records, passages)
    assert not false
    assert result["passage_recall"] == 1
    assert result["readable_denominator"] == 1
    assert result["exact_match"] == 1


def test_lpr_passage_recall_is_tracker_recall_not_ocr_recall() -> None:
    passages = [
        {"id": "p", "valid_passage": True, "readable": True, "expected_plate": "ABC123", "accepted_plates": []},
    ]
    records = [
        {"stage": stage, "passage_id": "p", "round_id": round_id}
        for round_id in range(1, 4)
        for stage in ("detector_hit", "track_seen", "lpr_eligible", "plate_detected")
    ]
    result, false = lpr_results(records, passages)
    assert not false
    assert result["passage_recall"] == 1
    assert result["passages"][0]["detected_rounds"] == 3
    assert result["passages"][0]["recognized_rounds"] == 0
    assert result["exact_match"] == 0


def test_lpr_exact_match_uses_passage_representative() -> None:
    passages = [
        {"id": "p", "valid_passage": True, "readable": True, "expected_plate": "ABC123", "accepted_plates": []},
    ]
    records = []
    for round_id, plate in enumerate(("ABC123", "ABC123", "ABC12B"), start=1):
        records.extend(
            [
                {"stage": "track_seen", "passage_id": "p", "round_id": round_id},
                {"stage": "event_published", "passage_id": "p", "round_id": round_id, "plate": plate, "score": 0.95},
            ]
        )
    result, _ = lpr_results(records, passages)
    assert result["passages"][0]["representative"] == "ABC123"
    assert result["passages"][0]["exact"] is True
    assert result["passages"][0]["consistency"] == pytest.approx(2 / 3)


def test_api_sqlite_consistency_reports_uncommitted_when_both_are_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"stage": "event_published", "track_id": "event-1", "plate": "ABC123"}]
    monkeypatch.setattr(passage_validator, "sqlite_events", lambda ids, path: {})
    monkeypatch.setattr(passage_validator, "api_event", lambda event_id: None)
    consistent, mismatches, uncommitted = api_sqlite_consistency(records, "unused.db")
    assert consistent
    assert mismatches == []
    assert uncommitted == [{"event_id": "event-1", "stage": "event_published"}]


def test_api_sqlite_consistency_rejects_one_sided_event(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"stage": "event_published", "track_id": "event-1", "plate": "ABC123"}]
    monkeypatch.setattr(passage_validator, "sqlite_events", lambda ids, path: {"event-1": {"data": {"recognized_license_plate": "ABC123"}}})
    monkeypatch.setattr(passage_validator, "api_event", lambda event_id: None)
    times = iter((0.0, 3.0))
    monkeypatch.setattr(passage_validator.time, "monotonic", lambda: next(times))
    consistent, mismatches, uncommitted = api_sqlite_consistency(records, "unused.db")
    assert not consistent
    assert mismatches == [{"event_id": "event-1", "reason": "missing_api_or_sqlite"}]
    assert uncommitted == []


def test_plate_variants_are_normalized() -> None:
    assert normalize_plate("bee-3975") == "BEE3975"


def test_funnel_reports_first_missing_stage() -> None:
    passage = {"id": "p", "valid_passage": True, "readable": False, "expected_plate": None, "accepted_plates": []}
    records = []
    for round_id in range(1, 4):
        records += [{"stage": stage, "passage_id": "p", "round_id": round_id} for stage in ("detector_hit", "track_seen", "lpr_eligible")]
    result, _ = lpr_results(records, [passage])
    assert result["passages"][0]["mismatch_reason"] == "plate_detected_miss"
