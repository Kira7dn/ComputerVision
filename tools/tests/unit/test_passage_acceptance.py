import json
from pathlib import Path

import pytest
import yaml

import tools.runtime.validate_platform_runtime as passage_validator
from tools.fixtures.prepare_passage_fixture import (
    group_composite_passages,
    load_manifest,
)
from tools.lib.passage_metrics import (
    match_by_time_and_bbox,
    normalize_plate,
    score_face_passages,
)
from tools.runtime.validate_platform_runtime import (
    api_sqlite_consistency,
    assign_records,
    collect_native_trace_clips,
    correlation_mismatches,
    face_results,
    false_passage_count,
    lpr_results,
    media_evidence_records,
    recognition_lifecycle_summary,
    trace_lifecycle_groups,
    update_anchor_state,
    validate_runtime_lpr_evidence,
    wait_file_quiescent,
    wait_recognition_idle,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "tools/fixtures/platform_passage_ground_truth.yaml"


def test_wait_file_quiescent_requires_stable_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "evidence.jsonl"
    manifest.write_text("{}\n", encoding="utf-8")

    assert wait_file_quiescent(manifest, timeout=0.2, stable_seconds=0.05) is True
    assert (
        wait_file_quiescent(
            tmp_path / "missing.jsonl", timeout=0.05, stable_seconds=0.01
        )
        is False
    )


def test_trace_lifecycle_groups_excludes_detector_observations() -> None:
    records = [
        {
            "pipeline": "detector",
            "trace_id": "detector:car_camera:observation-1",
            "stage": "detector_hit",
        },
        {
            "pipeline": "lpr",
            "trace_id": "lpr:car_camera:event-1",
            "track_id": "event-1",
            "stage": "track_seen",
            "source_pts": 1.0,
        },
        {
            "pipeline": "face",
            "trace_id": "face:face_camera:event-2:g1",
            "track_id": "event-2",
            "stage": "first_qualified_face",
            "source_pts": 1.0,
        },
    ]

    groups = trace_lifecycle_groups(records)

    assert set(groups) == {
        ("lpr", "lpr:car_camera:event-1"),
        ("face", "face:face_camera:event-2:g1"),
    }


def test_media_evidence_records_loads_lpr_and_face_utf8(tmp_path: Path) -> None:
    for pipeline, trace_id in (
        ("lpr", "lpr:car_camera:xe-đỏ"),
        ("face", "face:face_camera:người-1:g1"),
    ):
        manifest = tmp_path / pipeline / "evidence.jsonl"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "pipeline": pipeline,
                    "trace_id": trace_id,
                    "source_pts": 1.0,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    records = media_evidence_records(tmp_path)

    assert {record["trace_id"] for record in records} == {
        "lpr:car_camera:xe-đỏ",
        "face:face_camera:người-1:g1",
    }


def test_native_trace_clip_uses_frigate_api_without_replay_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {
            "pipeline": "lpr",
            "trace_id": "lpr:car_camera:event-1",
            "camera": "car_camera",
            "track_id": "event-1",
            "stage": "track_seen",
            "source_pts": 101.0,
        }
    ]
    requested: list[str] = []

    class Response:
        def __init__(self):
            self.headers = {"Content-Type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"native-frigate-clip"

    monkeypatch.setattr(
        passage_validator,
        "api_event",
        lambda event_id: {
            "id": event_id,
            "camera": "car_camera",
            "start_time": 100.0,
            "end_time": 102.0,
            "has_clip": True,
        },
    )
    monkeypatch.setattr(
        passage_validator,
        "sqlite_trace_media",
        lambda event_ids, database_path: {
            "event-1": {
                "event": {
                    "id": "event-1",
                    "camera": "car_camera",
                    "start_time": 100.0,
                    "end_time": 102.0,
                    "has_clip": True,
                },
                "recordings": [
                    {
                        "path": "/media/frigate/recordings/car_camera/segment.mp4",
                        "start_time": 99.0,
                        "end_time": 103.0,
                    }
                ],
            }
        },
    )
    monkeypatch.setattr(
        passage_validator,
        "sqlite_recording_ranges",
        lambda requests, database_path: {
            "lpr|lpr:car_camera:event-1": [
                {
                    "path": "/media/frigate/recordings/car_camera/segment.mp4",
                    "start_time": 99.0,
                    "end_time": 103.0,
                }
            ]
        },
    )

    def open_clip(url: str, timeout: float):
        requested.append(url)
        return Response()

    monkeypatch.setattr(passage_validator, "urlopen", open_clip)
    monkeypatch.setattr(
        passage_validator,
        "ffprobe_clip",
        lambda path: {"valid": True, "format": {"duration": "3.0"}},
    )

    result = collect_native_trace_clips(tmp_path, records, "runtime.db")

    trace_root = tmp_path / "media/lpr/lpr_car_camera_event-1"
    assert requested == [
        "http://127.0.0.1:5001/api/car_camera/start/99.500000/end/102.500000/clip.mp4"
    ]
    assert (trace_root / "clip.mp4").read_bytes() == b"native-frigate-clip"
    assert (trace_root / "trace.json").is_file()
    assert not (trace_root / "replay").exists()
    assert result["complete"] is True


def test_native_trace_clip_uses_source_pts_when_event_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {
            "pipeline": "face",
            "trace_id": "face:face_camera:event-2:g1",
            "camera": "face_camera",
            "track_id": "event-2",
            "stage": "track_seen",
            "source_pts": 200.0,
        }
    ]
    requested: list[str] = []

    class Response:
        def __init__(self):
            self.headers = {"Content-Type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"native-range-clip"

    monkeypatch.setattr(passage_validator, "api_event", lambda event_id: None)
    monkeypatch.setattr(
        passage_validator, "sqlite_trace_media", lambda event_ids, database_path: {}
    )
    monkeypatch.setattr(
        passage_validator,
        "sqlite_recording_ranges",
        lambda requests, database_path: {
            "face|face:face_camera:event-2:g1": [
                {
                    "path": "/media/frigate/recordings/face_camera/segment.mp4",
                    "start_time": 199.0,
                    "end_time": 201.0,
                }
            ]
        },
    )
    monkeypatch.setattr(
        passage_validator,
        "urlopen",
        lambda url, timeout: (requested.append(url) or Response()),
    )
    monkeypatch.setattr(
        passage_validator, "ffprobe_clip", lambda path: {"valid": True}
    )
    times = iter((0.0, 9.0))
    monkeypatch.setattr(passage_validator.time, "monotonic", lambda: next(times))

    result = collect_native_trace_clips(tmp_path, records, "runtime.db")

    trace_root = tmp_path / "media/face/face_face_camera_event-2_g1"
    metadata = json.loads((trace_root / "trace.json").read_text(encoding="utf-8"))
    assert requested == [
        "http://127.0.0.1:5001/api/face_camera/start/199.500000/end/200.500000/clip.mp4"
    ]
    assert metadata["clip_basis"] == "trace_source_pts"
    assert metadata["event_id"] is None
    assert metadata["clip_status"] == "recorded"
    assert (trace_root / "clip.mp4").read_bytes() == b"native-range-clip"
    assert result["complete"] is True


def test_terminal_only_record_does_not_create_media_trace() -> None:
    groups = trace_lifecycle_groups(
        [
            {
                "pipeline": "face",
                "trace_id": "face:face_camera:event-3:g2",
                "track_id": "event-3",
                "stage": "recognition_terminal",
            }
        ]
    )

    assert groups == {}


def manifest_value() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def write_manifest(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "passage.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def write_evidence_manifest(tmp_path: Path, records: list[dict]) -> Path:
    evidence_dir = tmp_path / "passage-evidence"
    evidence_dir.mkdir()
    for index, record in enumerate(records):
        if not record.pop("with_artifact", False):
            continue
        relative = Path("lpr") / str(record["evidence_id"]) / f"{index}.jpg"
        target = evidence_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"image-{index}".encode())
        record.update(
            {
                "artifact_path": relative.as_posix(),
                "artifact_sha256": passage_validator.sha256(target),
                "artifact_bytes": target.stat().st_size,
            }
        )
    (evidence_dir / "evidence.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return evidence_dir


def test_runtime_lpr_evidence_accepts_complete_pre_gate_invocation(tmp_path: Path) -> None:
    base = {
        "evidence_id": "attempt-1",
        "camera": "car_camera",
        "frame_time": 1.0,
        "track_id": "car-1",
    }
    evidence_dir = write_evidence_manifest(
        tmp_path,
        [
            {**base, "stage": "invocation"},
            {**base, "stage": "runtime_frame", "with_artifact": True},
            {
                **base,
                "stage": "runtime_frame_object_box",
                "with_artifact": True,
            },
            {
                **base,
                "stage": "eligibility_decision",
                "accepted": False,
                "reason": "no_position_changes",
            },
        ],
    )

    records, summary = validate_runtime_lpr_evidence(
        evidence_dir, {}, {}, {}
    )

    assert len(records) == 4
    assert summary["valid"] is True
    assert summary["invocations"] == 1
    assert summary["artifact_count"] == 2
    assert summary["errors"] == []


def test_runtime_lpr_evidence_rejects_missing_plate_crop(tmp_path: Path) -> None:
    base = {
        "evidence_id": "attempt-2",
        "camera": "car_camera",
        "frame_time": 1.0,
        "track_id": "car-2",
    }
    evidence_dir = write_evidence_manifest(
        tmp_path,
        [
            {**base, "stage": "invocation"},
            {**base, "stage": "runtime_frame", "with_artifact": True},
            {
                **base,
                "stage": "runtime_frame_object_box",
                "with_artifact": True,
            },
            {
                **base,
                "stage": "eligibility_decision",
                "accepted": True,
                "reason": "eligible",
            },
            {**base, "stage": "car_crop", "with_artifact": True},
            {**base, "stage": "plate_detector_input", "with_artifact": True},
            {
                **base,
                "stage": "plate_detector_result",
                "accepted": True,
                "reason": "plate_detected",
            },
            {**base, "stage": "ocr_plate_input", "with_artifact": True},
            {
                **base,
                "stage": "ocr_result",
                "accepted": False,
                "reason": "text_detector_empty",
                "text_box_count": 0,
            },
        ],
    )

    _, summary = validate_runtime_lpr_evidence(evidence_dir, {}, {}, {})

    assert summary["valid"] is False
    assert any("missing stage: plate_crop" in error for error in summary["errors"])


def test_runtime_lpr_evidence_requires_every_physical_passage(tmp_path: Path) -> None:
    base = {
        "evidence_id": "attempt-3",
        "camera": "car_camera",
        "frame_time": 100.0,
        "track_id": "car-3",
        "object_box": [0, 0, 100, 100],
    }
    evidence_dir = write_evidence_manifest(
        tmp_path,
        [
            {**base, "stage": "invocation"},
            {**base, "stage": "runtime_frame", "with_artifact": True},
            {
                **base,
                "stage": "runtime_frame_object_box",
                "with_artifact": True,
            },
            {
                **base,
                "stage": "eligibility_decision",
                "accepted": False,
                "reason": "no_position_changes",
            },
        ],
    )
    passages = [
        {
            "id": "seen",
            "start_s": 1.0,
            "end_s": 2.0,
            "bbox": [0, 0, 100, 100],
        },
        {
            "id": "missing",
            "start_s": 3.0,
            "end_s": 4.0,
            "bbox": [200, 0, 300, 100],
        },
    ]

    _, summary = validate_runtime_lpr_evidence(
        evidence_dir,
        {"car_camera": [100.0]},
        {"car_camera": 5.0},
        {"car_camera": passages},
    )

    assert summary["valid"] is False
    assert summary["missing_passages"] == ["missing"]


def test_fixture_lpr_ground_truth_uses_audited_physical_labels() -> None:
    passages = {
        passage["id"]: passage for passage in manifest_value()["lpr"]["passages"]
    }

    assert {
        passage_id: (
            passages[passage_id]["readable"],
            passages[passage_id]["expected_plate"],
        )
        for passage_id in (
            "lpr-01",
            "lpr-transit-01",
            "lpr-02",
            "lpr-trailer-pickup-01",
            "lpr-red-suv-01",
            "lpr-service-van-01",
            "lpr-rental-van-01",
            "lpr-05",
            "lpr-07",
            "lpr-chevy-pickup-01",
            "lpr-06",
        )
    } == {
        "lpr-01": (True, "619879"),
        "lpr-transit-01": (True, "C98191P"),
        "lpr-02": (True, "657648"),
        "lpr-trailer-pickup-01": (True, "7BN2396"),
        "lpr-red-suv-01": (True, "1073"),
        "lpr-service-van-01": (True, "3789"),
        "lpr-rental-van-01": (True, "C64457T"),
        "lpr-05": (True, "3B53567"),
        "lpr-07": (True, "FKH9211"),
        "lpr-chevy-pickup-01": (True, "XX6755"),
        "lpr-06": (True, "BEE3975"),
    }
    assert all(
        not passages[passage_id].get("accepted_plates")
        for passage_id in passages
    )


def test_overlapping_lpr_passages_share_composite_segments() -> None:
    grouped = group_composite_passages(
        manifest_value()["lpr"]["passages"], "lpr"
    )

    assert [[passage["id"] for passage in group] for group in grouped] == [
        ["lpr-01", "lpr-transit-01", "lpr-02"],
        ["lpr-trailer-pickup-01", "lpr-red-suv-01"],
        [
            "lpr-service-van-01",
            "lpr-rental-van-01",
            "lpr-05",
            "lpr-07",
            "lpr-chevy-pickup-01",
        ],
        ["lpr-06"],
    ]


def test_wait_recognition_idle_reads_lifecycle_and_evidence(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"embeddings":{"recognition_lifecycle":{"in_flight":0},"evidence":{"pinned":0}}}'

    monkeypatch.setattr(passage_validator, "urlopen", lambda *_args, **_kwargs: Response())
    assert wait_recognition_idle(0.01)


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
    assert len(lpr) == 11
    assert sum(p["readable"] for p in lpr) == 11
    assert all(p["bbox"] and p["roi"] for p in lpr)
    assert value["lpr"]["frame"] == {"width": 1820, "height": 1024, "fps": 5}
    assert value["lpr"]["source"].endswith(" (1024p).mp4")


def test_passage_rejects_duplicate_id(tmp_path: Path) -> None:
    value = manifest_value()
    value["lpr"]["passages"][0]["id"] = value["face"]["passages"][0]["id"]
    with pytest.raises(ValueError, match="globally unique"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_bad_window(tmp_path: Path) -> None:
    value = manifest_value()
    value["face"]["passages"][0]["end_s"] = value["face"]["passages"][0]["start_s"]
    with pytest.raises(ValueError, match="invalid face passage window"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_bbox_outside_frame(tmp_path: Path) -> None:
    value = manifest_value()
    value["face"]["passages"][0]["bbox"] = [0, 0, 1281, 100]
    with pytest.raises(ValueError, match="outside"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_lpr_bbox_outside_its_frame(tmp_path: Path) -> None:
    value = manifest_value()
    value["lpr"]["passages"][0]["bbox"] = [0, 0, 1821, 100]
    with pytest.raises(ValueError, match="outside the 1820x1024 frame"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_readable_plate_without_label(tmp_path: Path) -> None:
    value = manifest_value()
    value["lpr"]["passages"][0]["expected_plate"] = None
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


def test_assignment_preserves_runtime_passage_and_propagates_ground_truth() -> None:
    passages = [
        {"id": "lpr-01", "start_s": 0, "end_s": 2, "bbox": [0, 0, 100, 100]},
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 100.5,
            "stage": "detector_hit",
            "object_box": [0, 0, 100, 100],
            "track_id": "vehicle-1",
            "passage_id": "runtime-event-id",
        },
        {
            "camera": "car_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "vehicle-1",
            "passage_id": "runtime-event-id",
        },
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 3},
        {"car_camera": passages},
    )

    assert [record["passage_id"] for record in assigned] == ["lpr-01", "lpr-01"]
    assert all(
        record["recognition_passage_id"] == "runtime-event-id"
        for record in assigned
    )
    assert not correlation_mismatches(assigned)


def test_time_only_record_cannot_seed_ground_truth_track_mapping() -> None:
    passages = [
        {"id": "lpr-01", "start_s": 0, "end_s": 2, "bbox": [0, 0, 100, 100]},
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 100.5,
            "stage": "recognition_attempt",
            "track_id": "other-vehicle",
            "passage_id": "runtime-id",
        },
        {
            "camera": "car_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "other-vehicle",
            "passage_id": "runtime-id",
        },
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 3},
        {"car_camera": passages},
    )
    assert assigned
    assert all(not record.get("passage_id") for record in assigned)


def test_conflicting_track_mapping_fails_closed_for_entire_track() -> None:
    passages = [
        {"id": "left", "start_s": 0, "end_s": 2, "bbox": [0, 0, 100, 100]},
        {"id": "right", "start_s": 0, "end_s": 2, "bbox": [500, 0, 600, 100]},
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 100.5,
            "stage": "detector_hit",
            "object_box": [0, 0, 100, 100],
            "track_id": "reused-track",
        },
        {
            "camera": "car_camera",
            "frame_time": 100.6,
            "stage": "detector_hit",
            "object_box": [500, 0, 600, 100],
            "track_id": "reused-track",
        },
        {
            "camera": "car_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "reused-track",
        },
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 3},
        {"car_camera": passages},
    )

    assert all(not record.get("passage_id") for record in assigned)


def test_moving_track_is_locked_to_one_physical_passage() -> None:
    passages = [
        {
            "id": "fkh",
            "start_s": 1,
            "end_s": 3,
            "bbox": [925, 0, 1220, 205],
        },
        {
            "id": "xx",
            "start_s": 1,
            "end_s": 3,
            "bbox": [490, 0, 840, 250],
        },
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 101.1,
            "stage": "event_published",
            "object_box": [937, 0, 1170, 204],
            "track_id": "moving-car",
            "plate": "FKH9211",
        },
        {
            "camera": "car_camera",
            "frame_time": 101.7,
            "stage": "event_published",
            "object_box": [742, 168, 1103, 509],
            "track_id": "moving-car",
            "plate": "FKH9211",
        },
        {
            "camera": "car_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "moving-car",
        },
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 4},
        {"car_camera": passages},
    )

    assert {record.get("passage_id") for record in assigned} == {"fkh"}


def test_recognition_evidence_overrides_stale_initial_track_box() -> None:
    passages = [
        {"id": "early", "start_s": 1, "end_s": 2, "bbox": [0, 0, 100, 100]},
        {"id": "late", "start_s": 3, "end_s": 4, "bbox": [500, 0, 600, 100]},
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 100.0,
            "stage": "track_seen",
            "object_box": [0, 0, 100, 100],
            "track_id": "continued-track",
        },
        {
            "camera": "car_camera",
            "frame_time": 102.0,
            "stage": "event_published",
            "object_box": [500, 0, 600, 100],
            "track_id": "continued-track",
            "plate": "BEE3975",
        },
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 5},
        {"car_camera": passages},
    )

    assert {record.get("passage_id") for record in assigned} == {"late"}


def test_untracked_record_with_ambiguous_bbox_fails_closed() -> None:
    passages = [
        {"id": "a", "start_s": 1, "end_s": 2, "bbox": [0, 0, 100, 100]},
        {"id": "b", "start_s": 1, "end_s": 2, "bbox": [10, 0, 110, 100]},
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 101.5,
            "stage": "detector_hit",
            "object_box": [5, 0, 105, 100],
        }
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 3},
        {"car_camera": passages},
    )

    assert len(assigned) == 1
    assert not assigned[0].get("passage_id")


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


def test_face_passage_reports_single_replay_round() -> None:
    passage = {
        "id": "known",
        "valid_passage": True,
        "start_s": 1.0,
        "expected_identity": "Jack",
    }
    records = []
    for round_id, anchor in ((1, 100.0),):
        records.extend(
            [
                {"passage_id": "known", "round_id": round_id, "stage": "first_qualified_face", "trace_time": anchor + 0.1, "frame_time": anchor + 0.1},
                {"passage_id": "known", "round_id": round_id, "stage": "first_attempt", "identity": "Jack", "trace_time": anchor + 0.2, "frame_time": anchor + 0.2},
                {"passage_id": "known", "round_id": round_id, "stage": "confirmed_result", "identity": "Jack", "trace_time": anchor + 0.3, "frame_time": anchor + 0.3},
            ]
        )
    result, _, *_ = face_results(records, [passage], [100.0])
    assert result["passages"][0]["detected_rounds"] == 1
    assert result["passages"][0]["correct_rounds"] == 1
    assert result["detection_recall"] == 1
    assert result["accuracy"] == 1
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
    assert result["accuracy"] == 1
    assert result["recognition_publish_count"] == 2
    assert result["precision"] == 0.5
    assert result["recall"] == 1
    assert result["exact_match"] == 1


def test_lpr_passage_recall_is_tracker_recall_not_ocr_recall() -> None:
    passages = [
        {"id": "p", "valid_passage": True, "readable": True, "expected_plate": "ABC123", "accepted_plates": []},
    ]
    records = [
        {"stage": stage, "passage_id": "p", "round_id": round_id}
        for round_id in range(1, 2)
        for stage in ("detector_hit", "track_seen", "lpr_eligible", "plate_detected")
    ]
    result, false = lpr_results(records, passages)
    assert not false
    assert result["passage_recall"] == 1
    assert result["passages"][0]["detected_rounds"] == 1
    assert result["passages"][0]["recognized_rounds"] == 0
    assert result["accuracy"] == 0
    assert result["precision"] == 1
    assert result["recall"] == 0
    assert result["exact_match"] == 0


def test_wrong_publish_is_recognition_fp_and_readable_passage_fn() -> None:
    passages = [
        {
            "id": "p",
            "valid_passage": True,
            "readable": True,
            "expected_plate": "ABC123",
            "accepted_plates": [],
        }
    ]
    records = [
        {"stage": "track_seen", "passage_id": "p", "round_id": round_id}
        for round_id in range(1, 4)
    ] + [
        {
            "stage": "event_published",
            "passage_id": "p",
            "round_id": round_id,
            "plate": "WRONG9",
        }
        for round_id in range(1, 4)
    ]
    result, _ = lpr_results(records, passages)
    assert result["passage_precision"] == 1
    assert result["passage_recall"] == 1
    assert result["accuracy"] == 0
    assert result["precision"] == 0
    assert result["recall"] == 0


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


def test_recognition_lifecycle_summary_detects_attempts_duplicates_and_early_stop() -> None:
    records = []
    for task in ("face", "lpr"):
        for index in (1, 2):
            records.append(
                {
                    "stage": "recognition_attempt",
                    "task": task,
                    "camera": f"{task}_camera",
                    "track_id": "track",
                    "generation": 1,
                    "attempt_index": index,
                    "candidate_id": f"{task}-{index}",
                    "latency_ms": 10,
                }
            )
        records.append(
            {
                "stage": "recognition_terminal",
                "task": task,
                "camera": f"{task}_camera",
                "track_id": "track",
                "generation": 1,
                "status": "ACCEPTED",
                "reason": "consensus_accepted",
            }
        )
    summary = recognition_lifecycle_summary(records, {"in_flight": 0})
    assert summary["max_attempts_per_track"] == 2
    assert summary["early_stop_by_task"] == {"face": True, "lpr": True}
    assert not summary["duplicate_inference"]

    records[1]["candidate_id"] = records[0]["candidate_id"]
    summary = recognition_lifecycle_summary(records, {})
    assert summary["duplicate_inference"]
