import json
from pathlib import Path

import pytest
import yaml

import tools.runtime.validate_platform_runtime as passage_validator
from tools.fixtures.prepare_passage_fixture import (
    face_source,
    load_manifest,
    snapshot_face_library,
)
from tools.lib.passage_metrics import (
    match_by_time_and_bbox,
    normalize_plate,
    score_face_passages,
)
from tools.runtime.validate_platform_runtime import (
    annotate_face_evidence,
    api_sqlite_consistency,
    assign_records,
    audit_tracker_lifecycle_rows,
    collect_native_trace_clips,
    correlation_mismatches,
    face_results,
    face_trace_outcome,
    false_passage_count,
    finalize_finite_source_tracks,
    lpr_results,
    media_evidence_records,
    pipeline_trace_ids,
    recognition_lifecycle_summary,
    restore_mounts_verified,
    source_pts_metrics,
    trace_lifecycle_groups,
    validate_external_recognition_evidence,
    validate_runtime_lpr_evidence,
    wait_file_quiescent,
    wait_recognition_idle,
    write_compact_runtime_report,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "tools/fixtures/platform_passage_ground_truth.yaml"


def test_tracker_topology_config_uses_direct_node_map(monkeypatch) -> None:
    monkeypatch.setattr(passage_validator, "create_service_tls", lambda *args: None)
    config: dict[str, object] = {}

    passage_validator.configure_tracker_topology(config, "tracker")

    tracker = config["tracker"]
    assert isinstance(tracker, dict)
    assert "edge-local" in tracker
    assert "nodes" not in tracker


def test_interrupted_face_attempt_accepts_typed_failure(tmp_path: Path) -> None:
    trace_id = "face:face_camera:1.0-track"
    records = [
        {"pipeline": "face", "stage": "track_seen", "trace_id": trace_id},
        {"pipeline": "face", "stage": "first_attempt", "trace_id": trace_id, "frame_time": 1.0},
        {
            "pipeline": "face",
            "stage": "recognition_failed",
            "trace_id": trace_id,
            "reason": "service_disconnected",
        },
    ]

    result = validate_external_recognition_evidence(tmp_path, records, [])

    assert result["valid"] is True
    assert result["attempt_count"] == 1
    assert result["errors"] == []


def test_face_attempt_without_typed_failure_still_requires_artifacts(tmp_path: Path) -> None:
    trace_id = "face:face_camera:2.0-track"
    records = [
        {"pipeline": "face", "stage": "track_seen", "trace_id": trace_id},
        {"pipeline": "face", "stage": "first_attempt", "trace_id": trace_id, "frame_time": 2.0},
    ]

    result = validate_external_recognition_evidence(tmp_path, records, [])

    assert result["valid"] is False
    assert any("missing stage: recognition_attempt" in error for error in result["errors"])


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


def test_finalize_finite_source_tracks_uses_raw_ids_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_docker_output(*args: object, **kwargs: object) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(passage_validator, "docker_output", fake_docker_output)
    records = [
        {
            "pipeline": "face",
            "stage": "track_seen",
            "track_id": "raw-face-id",
            "camera": "face_camera",
        },
        {
            "pipeline": "face",
            "stage": "track_seen",
            "track_id": "raw-face-id",
            "camera": "face_camera",
        },
        {
            "pipeline": "lpr",
            "stage": "track_seen",
            "track_id": "raw-lpr-id",
            "camera": "car_camera",
        },
        {
            "pipeline": "detector",
            "stage": "track_seen",
            "track_id": "ignored",
            "camera": "car_camera",
        },
    ]

    assert finalize_finite_source_tracks(records) == 2
    payload = json.loads(str(calls[0][5]))
    assert payload == [
        ["raw-face-id", "face_camera"],
        ["raw-lpr-id", "car_camera"],
    ]


@pytest.mark.parametrize(
    "docker_source",
    [
        "D:/BusinessAnalyze/Camera/deploy/config.yaml",
        "/run/desktop/mnt/host/d/BusinessAnalyze/Camera/deploy/config.yaml",
    ],
)
def test_restore_mount_verification_accepts_windows_docker_path_forms(
    docker_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_docker_output(*args: object, **kwargs: object) -> str:
        if "{{.State.Running}}" in args:
            return "true"
        return json.dumps(
            [
                {
                    "Source": docker_source,
                    "Destination": "/config/config.yml",
                }
            ]
        )

    monkeypatch.setattr(passage_validator, "docker_output", fake_docker_output)

    assert restore_mounts_verified(ROOT / "deploy/config.yaml") is True


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


def test_pipeline_trace_ids_keeps_raw_lpr_ids_without_fixture_merging() -> None:
    records = [
        {"pipeline": "detector", "trace_id": "detector:car_camera:1"},
        {"pipeline": "lpr", "trace_id": "lpr:car_camera:track-1"},
        {"pipeline": "lpr", "trace_id": "lpr:car_camera:track-1"},
        {"pipeline": "lpr", "trace_id": "lpr:car_camera:track-2"},
        {"pipeline": "face", "trace_id": "face:face_camera:track-3"},
    ]

    assert pipeline_trace_ids(records, "lpr") == [
        "lpr:car_camera:track-1",
        "lpr:car_camera:track-2",
    ]


def test_face_trace_outcome_counts_unknown_as_completed_recognition() -> None:
    assert (
        face_trace_outcome(
            [
                {"stage": "track_seen"},
                {"stage": "first_attempt", "identity": "unknown", "score": 0.02},
            ]
        )
        == "recognized_unknown"
    )
    assert face_trace_outcome([{"stage": "track_seen"}]) == "not_recognized"


def test_compact_report_has_one_canonical_table_per_pipeline(
    tmp_path: Path,
) -> None:
    runtime_records = [
        {
            "pipeline": "lpr",
            "trace_id": "lpr:car_camera:lpr-1",
            "stage": "track_seen",
            "source_pts": 1.0,
        },
        {
            "pipeline": "face",
            "trace_id": "face:face_camera:face-fail",
            "stage": "track_seen",
            "source_pts": 2.0,
        },
        {
            "pipeline": "face",
            "trace_id": "face:face_camera:face-fail",
            "stage": "first_qualified_face",
            "source_pts": 2.05,
        },
        {
            "pipeline": "face",
            "trace_id": "face:face_camera:face-fail",
            "stage": "first_attempt",
            "identity": "unknown",
            "score": 0.02,
            "status": "unknown",
            "source_pts": 2.1,
        },
    ]
    (tmp_path / "runtime-trace.json").write_text(
        json.dumps({"records": runtime_records}), encoding="utf-8"
    )
    summary = {
        "report": {"status": "complete"},
        "measurement": {"measurement_valid": False},
        "runtime": {"recognition": {}, "resources": {}, "source_pts": {}},
        "lpr": {"passages": []},
        "face": {"passages": []},
    }

    report = write_compact_runtime_report(tmp_path, summary).read_text(encoding="utf-8")

    assert "## Run" in report
    assert "## LPR result" in report
    assert "## Face result" in report
    assert report.count("## LPR result") == 1
    assert report.count("## Face result") == 1
    assert "| Clip | Outcome | Track | Eligible | Plate | OCR | Publish |" in report
    assert (
        "| Clip | Outcome | Prepare face | Recognition | Decision / publish |"
        in report
    )
    assert "| Admission | Prepare | Classify | Vote | Publish |" not in report
    assert "| Fixture | Expected | Runtime trace / clip |" not in report
    assert "| Runtime trace / clip | BBox | Seen | Attempts |" not in report
    assert "recognized_unknown" in report
    assert "face-fail" in report
    assert "Face recognition outcome index" not in report
    assert report.count("### Lifecycle traces") == 2
    assert "`face:face_camera:face-fail` — `recognized_unknown`" in report
    assert (
        report.count(
            "| Stage | Records | Source PTS | Status | Final result | Image |"
        )
        == 2
    )
    assert "| Attempt | Source PTS | Prepare face |" not in report
    assert "Throughput and source timing" not in report
    assert "Provenance and artifacts" not in report
    assert "Runtime diagnostics" not in report
    assert "[0 LPR images](media/images.md#lpr)" in report
    assert "[0 artifacts](media/images.md#face)" in report
    assert (tmp_path / "media" / "images.md").is_file()


def test_source_pts_completeness_ignores_non_frame_terminal_records() -> None:
    result = source_pts_metrics(
        [
            {
                "camera": "face_camera",
                "stage": "first_attempt",
                "frame_time": 10.0,
                "source_pts": 10.0,
            },
            {
                "camera": "face_camera",
                "stage": "recognition_terminal",
                "frame_time": None,
                "source_pts": None,
            },
        ]
    )

    assert result["face_camera"]["missing_source_pts"] == 0
    assert result["face_camera"]["records_without_frame"] == 1


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


def test_face_bbox_evidence_is_producer_owned_and_not_rewritten(
    tmp_path: Path,
) -> None:
    image = passage_validator.np.zeros((80, 120, 3), dtype=passage_validator.np.uint8)
    raw_path = (
        tmp_path / "face" / "trace-1" / "evidence-1" / "00001-recognition_attempt.jpg"
    )
    raw_path.parent.mkdir(parents=True)
    assert passage_validator.cv2.imwrite(str(raw_path), image)
    raw_hash = passage_validator.sha256(raw_path)
    records = [
        {
            "pipeline": "face",
            "stage": "recognition_attempt_bbox",
            "trace_id": "face:face_camera:trace-1",
            "artifact_path": raw_path.relative_to(tmp_path).as_posix(),
            "object_box": [10, 10, 100, 70],
            "detail_box": [40, 20, 65, 50],
            "raw_identity": "unknown",
            "raw_score": 0.12,
        }
    ]

    result = annotate_face_evidence(tmp_path, records)

    assert result["valid"] is True
    assert result["eligible_records"] == result["annotated_count"] == 1
    assert passage_validator.sha256(raw_path) == raw_hash
    annotated_path = tmp_path / result["records"][0]["annotated_artifact_path"]
    assert annotated_path.is_file()
    assert annotated_path == raw_path
    assert passage_validator.sha256(annotated_path) == raw_hash
    assert result["records"][0]["annotation_mode"] == "producer_owned_bbox_image"
    assert result["records"][0]["object_box"] == [10, 10, 100, 70]
    assert result["records"][0]["detail_box"] == [40, 20, 65, 50]


def test_annotate_face_evidence_fails_closed_without_bbox(tmp_path: Path) -> None:
    raw_path = tmp_path / "face" / "raw.jpg"
    raw_path.parent.mkdir(parents=True)
    image = passage_validator.np.zeros((10, 10, 3), dtype=passage_validator.np.uint8)
    assert passage_validator.cv2.imwrite(str(raw_path), image)

    result = annotate_face_evidence(
        tmp_path,
        [
            {
                "pipeline": "face",
                "stage": "recognition_attempt_bbox",
                "artifact_path": "face/raw.jpg",
            }
        ],
    )

    assert result["valid"] is False
    assert result["missing_bbox"] == 1


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
        "sqlite_recordings_for_trace_ranges",
        lambda ranges, database_path: {
            "lpr\0lpr:car_camera:event-1": [
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
        "http://127.0.0.1:5001/api/car_camera/start/100.750000/end/101.750000/clip.mp4"
    ]
    trace = json.loads((trace_root / "trace.json").read_text(encoding="utf-8"))
    assert trace["clip_basis"] == "trace_lifecycle"
    assert (trace_root / "clip.mp4").read_bytes() == b"native-frigate-clip"
    assert (trace_root / "trace.json").is_file()
    assert not (trace_root / "replay").exists()
    assert result["complete"] is True


def test_native_trace_clip_uses_recording_when_event_is_absent(
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
        headers = {"Content-Type": "video/mp4"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"native-recording-without-event"

    monkeypatch.setattr(passage_validator, "api_event", lambda event_id: None)
    monkeypatch.setattr(
        passage_validator, "sqlite_trace_media", lambda event_ids, database_path: {}
    )
    monkeypatch.setattr(
        passage_validator,
        "sqlite_recordings_for_trace_ranges",
        lambda ranges, database_path: {
            "face\0face:face_camera:event-2:g1": [
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
        lambda url, timeout: requested.append(url) or Response(),
    )
    monkeypatch.setattr(
        passage_validator,
        "ffprobe_clip",
        lambda path: {"valid": True, "format": {"duration": "1.0"}},
    )
    times = iter((0.0, 9.0))
    monkeypatch.setattr(passage_validator.time, "monotonic", lambda: next(times))

    result = collect_native_trace_clips(tmp_path, records, "runtime.db")

    trace_root = tmp_path / "media/face/face_face_camera_event-2_g1"
    metadata = json.loads((trace_root / "trace.json").read_text(encoding="utf-8"))
    assert requested == [
        "http://127.0.0.1:5001/api/face_camera/start/199.750000/end/200.750000/clip.mp4"
    ]
    assert metadata["event_id"] is None
    assert metadata["clip_status"] == "recorded"
    assert metadata["clip_reason"] is None
    assert (trace_root / "clip.mp4").read_bytes() == b"native-recording-without-event"
    assert result["complete"] is True


def test_edge_trace_media_uses_event_proxy_snapshot_and_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        {
            "pipeline": "lpr",
            "trace_id": "lpr:car_camera:event-1",
            "camera": "car_camera",
            "event_id": "event-1",
            "stage": "track_seen",
            "source_pts": 100.0,
        }
    ]

    monkeypatch.setattr(
        passage_validator,
        "api_event",
        lambda event_id: {
            "id": event_id,
            "camera": "car_camera",
            "start_time": 99.0,
            "end_time": 101.0,
            "has_clip": True,
        },
    )
    monkeypatch.setattr(
        passage_validator, "sqlite_trace_media", lambda event_ids, path: {}
    )
    monkeypatch.setattr(
        passage_validator,
        "sqlite_recordings_for_trace_ranges",
        lambda ranges, path: {},
    )

    class Response:
        def __init__(self, payload: bytes, content_type: str, status: int = 200):
            self._payload = payload
            self.status = status
            self.headers = {"Content-Type": content_type}
            if status == 206:
                self.headers["Content-Range"] = "bytes 0-1023/4096"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self._payload

    def open_media(request, timeout):
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("snapshot.jpg"):
            return Response(b"jpeg", "image/jpeg")
        if hasattr(request, "headers") and request.headers.get("Range"):
            return Response(b"range", "video/mp4", 206)
        return Response(b"edge-clip", "video/mp4")

    monkeypatch.setattr(passage_validator, "urlopen", open_media)
    monkeypatch.setattr(
        passage_validator,
        "ffprobe_clip",
        lambda path: {"valid": True, "format": {"duration": "2.0"}},
    )

    result = collect_native_trace_clips(
        tmp_path, records, "runtime.db", edge_owned=True
    )

    trace = result["traces"][0]
    assert trace["clip_basis"] == "edge_media_manifest"
    assert trace["range_proxy_status"] == 206
    assert trace["snapshot_proxy_content_type"] == "image/jpeg"
    assert trace["media_proxy_complete"] is True
    assert result["complete"] is True
    assert result["media_proxy_complete"] is True


def test_tracker_lifecycle_audit_accepts_one_ordered_producer_event() -> None:
    common = {
        "node_id": "edge-local",
        "node_epoch": "epoch-1",
        "camera_id": "car_camera",
        "stream_epoch": "stream-1",
        "event_id": "event-1",
        "track_id": "raw-7",
    }
    result = audit_tracker_lifecycle_rows(
        [
            {**common, "journal_sequence": 1, "operation": "START"},
            {**common, "journal_sequence": 2, "operation": "UPDATE"},
            {**common, "journal_sequence": 3, "operation": "END"},
        ]
    )
    assert result["valid"] is True
    assert result["event_count"] == 1
    assert result["active_count"] == 0


def test_tracker_lifecycle_audit_rejects_duplicate_and_missing_end() -> None:
    common = {
        "node_id": "edge-local",
        "node_epoch": "epoch-1",
        "camera_id": "car_camera",
        "stream_epoch": "stream-1",
        "event_id": "event-1",
        "track_id": "raw-7",
        "operation": "START",
        "journal_sequence": 1,
    }
    result = audit_tracker_lifecycle_rows([common, common])
    assert result["valid"] is False
    assert result["active_count"] == 1
    assert any(error.startswith("duplicate_sequence") for error in result["errors"])
    assert any(error.startswith("end_count") for error in result["errors"])


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


def test_runtime_lpr_evidence_accepts_complete_pre_gate_invocation(
    tmp_path: Path,
) -> None:
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

    records, summary = validate_runtime_lpr_evidence(evidence_dir, {}, {}, {})

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


def test_runtime_lpr_evidence_is_validated_per_pipeline_invocation(
    tmp_path: Path,
) -> None:
    base = {
        "evidence_id": "attempt-3",
        "camera": "car_camera",
        "frame_time": 101.5,
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

    assert summary["valid"] is True
    assert "missing_passages" not in summary


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
            "lpr-02",
            "lpr-03",
            "lpr-04",
            "lpr-05",
            "lpr-06",
            "lpr-07",
            "lpr-08",
            "lpr-09",
            "lpr-10",
            "lpr-11",
        )
    } == {
        "lpr-01": (True, "619879"),
        "lpr-02": (True, "C98191P"),
        "lpr-03": (True, "657648"),
        "lpr-04": (True, "7BN2396"),
        "lpr-05": (True, "1073"),
        "lpr-06": (True, "3789"),
        "lpr-07": (True, "C64457T"),
        "lpr-08": (True, "3B53567"),
        "lpr-09": (True, "FKH9211"),
        "lpr-10": (True, "XX6755"),
        "lpr-11": (True, "BEE3975"),
    }
    assert all(
        not passages[passage_id].get("accepted_plates") for passage_id in passages
    )


def test_wait_recognition_idle_reads_lifecycle_and_evidence(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"embeddings":{"recognition_lifecycle":{"in_flight":0},"evidence":{"pinned":0}}}'

    monkeypatch.setattr(
        passage_validator, "urlopen", lambda *_args, **_kwargs: Response()
    )
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
    lpr = [p for p in value["lpr"]["passages"] if p.get("valid_passage", True)]
    assert value["face"]["source"] == (
        "mock_videos/face-recognition/segments/01_P1E_S1_C1_5s-20s.mp4"
    )
    assert "passages" not in value["face"]
    assert "close_follow" not in value["face"]
    assert "enrollment" not in value["face"]
    assert "trim" not in value["face"]
    source, windows = face_source(value, ROOT)
    assert source == (ROOT / value["face"]["source"]).resolve()
    assert windows == []
    assert len(lpr) == 11
    assert sum(p["readable"] for p in lpr) == 11
    assert all("bbox" not in p and "roi" not in p for p in lpr)
    assert all("start_s" not in p and "end_s" not in p for p in lpr)
    assert value["lpr"]["frame"] == {"width": 1820, "height": 1024, "fps": 5}
    assert value["lpr"]["source"].endswith(" (1024p).mp4")


def test_passage_rejects_duplicate_lpr_id(tmp_path: Path) -> None:
    value = manifest_value()
    value["lpr"]["passages"][1]["id"] = value["lpr"]["passages"][0]["id"]
    with pytest.raises(ValueError, match="globally unique"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_face_fixture_rejects_passage_injection(tmp_path: Path) -> None:
    value = manifest_value()
    value["face"]["passages"] = [{"id": "forbidden"}]
    with pytest.raises(ValueError, match="must not define passages"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_face_fixture_rejects_synthetic_enrollment(tmp_path: Path) -> None:
    value = manifest_value()
    value["face"]["enrollment"] = {
        "identity": "Jack",
        "source": value["face"]["source"],
        "frame_s": 21.125,
    }
    with pytest.raises(ValueError, match="configured face library snapshot"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_snapshot_face_library_copies_identities_and_excludes_train(
    tmp_path: Path,
) -> None:
    source = tmp_path / "production-media" / "clips" / "faces"
    for relative, content in (
        (Path("Alice") / "one.jpg", b"alice"),
        (Path("Bob") / "two.webp", b"bob"),
        (Path("train") / "attempt.webp", b"training"),
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (source / "Alice" / "ignored.txt").write_text("ignored", encoding="utf-8")
    destination = tmp_path / "isolated-media"

    result = snapshot_face_library(
        {"runtime": {"media_dir": str(tmp_path / "production-media")}},
        tmp_path,
        destination,
    )

    assert result["identity_count"] == 2
    assert result["image_count"] == 2
    assert result["train_copied"] is False
    assert len(result["sha256"]) == 64
    assert (
        destination / "clips" / "faces" / "Alice" / "one.jpg"
    ).read_bytes() == b"alice"
    assert (destination / "clips" / "faces" / "Bob" / "two.webp").read_bytes() == b"bob"
    assert not (destination / "clips" / "faces" / "train").exists()


def test_passage_rejects_duplicate_lpr_expected_plate(tmp_path: Path) -> None:
    value = manifest_value()
    value["lpr"]["passages"][1]["expected_plate"] = value["lpr"]["passages"][0][
        "expected_plate"
    ]
    with pytest.raises(ValueError, match="expected plates must be unique"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_passage_rejects_readable_plate_without_label(tmp_path: Path) -> None:
    value = manifest_value()
    value["lpr"]["passages"][0]["expected_plate"] = None
    with pytest.raises(ValueError, match="requires uppercase alphanumeric"):
        load_manifest(write_manifest(tmp_path, value), ROOT)


def test_runtime_has_no_controlled_replay_publisher() -> None:
    validator = (ROOT / "tools/runtime/validate_platform_runtime.py").read_text(
        encoding="utf-8"
    )
    deploy = (ROOT / "deploy/run.ps1").read_text(encoding="utf-8")
    assert "CAMERA_REPLAY_CONTROLLED" not in validator + deploy
    assert "camera-replay-car-camera" not in validator


def test_fixture_closes_missing_face_track_before_later_person() -> None:
    builder = (ROOT / "tools/fixtures/prepare_passage_fixture.py").read_text(
        encoding="utf-8"
    )

    assert 'config["cameras"]["face_camera"]["detect"]["fps"] = 15' in builder
    assert (
        'config["cameras"]["face_camera"]["detect"]["max_disappeared"] = 15' in builder
    )


def test_time_and_bbox_distinguish_simultaneous_vehicles() -> None:
    passages = [
        {"id": "a", "start_s": 1, "end_s": 2, "bbox": [0, 0, 100, 100]},
        {"id": "b", "start_s": 1, "end_s": 2, "bbox": [500, 0, 600, 100]},
    ]
    matched = match_by_time_and_bbox(
        [{"frame_time": 1.5, "bbox": [505, 5, 595, 95]}], passages
    )
    assert list(matched) == ["b"]

    records = [
        {
            "camera": "face_camera",
            "frame_time": 101.5,
            "stage": "event_published",
            "object_box": [505, 5, 595, 95],
            "track_id": "t",
        }
    ]
    assigned = assign_records(
        records, {"face_camera": [100]}, {"face_camera": 3}, {"face_camera": passages}
    )
    assert assigned[0]["passage_id"] == "b"


def test_assignment_preserves_runtime_passage_and_propagates_ground_truth() -> None:
    passages = [
        {"id": "lpr-01", "start_s": 0, "end_s": 2, "bbox": [0, 0, 100, 100]},
    ]
    records = [
        {
            "camera": "face_camera",
            "frame_time": 100.5,
            "stage": "detector_hit",
            "object_box": [0, 0, 100, 100],
            "track_id": "vehicle-1",
            "passage_id": "runtime-event-id",
        },
        {
            "camera": "face_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "vehicle-1",
            "passage_id": "runtime-event-id",
        },
    ]

    assigned = assign_records(
        records,
        {"face_camera": [100]},
        {"face_camera": 3},
        {"face_camera": passages},
    )

    assert [record["passage_id"] for record in assigned] == ["lpr-01", "lpr-01"]
    assert all(
        record["recognition_passage_id"] == "runtime-event-id" for record in assigned
    )
    assert not correlation_mismatches(assigned)


def test_time_only_record_cannot_seed_ground_truth_track_mapping() -> None:
    passages = [
        {"id": "lpr-01", "start_s": 0, "end_s": 2, "bbox": [0, 0, 100, 100]},
    ]
    records = [
        {
            "camera": "face_camera",
            "frame_time": 100.5,
            "stage": "recognition_attempt",
            "track_id": "other-vehicle",
            "passage_id": "runtime-id",
        },
        {
            "camera": "face_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "other-vehicle",
            "passage_id": "runtime-id",
        },
    ]

    assigned = assign_records(
        records,
        {"face_camera": [100]},
        {"face_camera": 3},
        {"face_camera": passages},
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
            "camera": "face_camera",
            "frame_time": 100.5,
            "stage": "detector_hit",
            "object_box": [0, 0, 100, 100],
            "track_id": "reused-track",
        },
        {
            "camera": "face_camera",
            "frame_time": 100.6,
            "stage": "detector_hit",
            "object_box": [500, 0, 600, 100],
            "track_id": "reused-track",
        },
        {
            "camera": "face_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "reused-track",
        },
    ]

    assigned = assign_records(
        records,
        {"face_camera": [100]},
        {"face_camera": 3},
        {"face_camera": passages},
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
            "camera": "face_camera",
            "frame_time": 101.1,
            "stage": "event_published",
            "object_box": [937, 0, 1170, 204],
            "track_id": "moving-car",
            "plate": "FKH9211",
        },
        {
            "camera": "face_camera",
            "frame_time": 101.7,
            "stage": "event_published",
            "object_box": [742, 168, 1103, 509],
            "track_id": "moving-car",
            "plate": "FKH9211",
        },
        {
            "camera": "face_camera",
            "round_id": 1,
            "stage": "recognition_terminal",
            "track_id": "moving-car",
        },
    ]

    assigned = assign_records(
        records,
        {"face_camera": [100]},
        {"face_camera": 4},
        {"face_camera": passages},
    )

    assert {record.get("passage_id") for record in assigned} == {"fkh"}


def test_face_recognition_evidence_overrides_stale_initial_track_box() -> None:
    passages = [
        {"id": "early", "start_s": 1, "end_s": 2, "bbox": [0, 0, 100, 100]},
        {"id": "late", "start_s": 3, "end_s": 4, "bbox": [500, 0, 600, 100]},
    ]
    records = [
        {
            "camera": "face_camera",
            "frame_time": 101.5,
            "stage": "track_seen",
            "object_box": [0, 0, 100, 100],
            "track_id": "continued-track",
        },
        {
            "camera": "face_camera",
            "frame_time": 103.5,
            "stage": "event_published",
            "object_box": [500, 0, 600, 100],
            "track_id": "continued-track",
            "plate": "BEE3975",
        },
    ]

    assigned = assign_records(
        records,
        {"face_camera": [100]},
        {"face_camera": 5},
        {"face_camera": passages},
    )

    assert {record.get("passage_id") for record in assigned} == {"late"}


def test_lpr_assignment_uses_only_published_plate() -> None:
    passages = [
        {
            "id": "expected-plate",
            "start_s": 8,
            "end_s": 9,
            "bbox": [900, 900, 1000, 1000],
            "expected_plate": "XX6755",
        }
    ]
    records = [
        {
            "camera": "car_camera",
            "frame_time": 100.1,
            "stage": "event_published",
            "object_box": [0, 0, 100, 100],
            "track_id": "pipeline-trace",
            "plate": "XX6755",
        }
    ]

    assigned = assign_records(
        records,
        {"car_camera": [100]},
        {"car_camera": 10},
        {"car_camera": passages},
    )

    assert assigned[0]["passage_id"] == "expected-plate"


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
        {
            "id": "known",
            "start_s": 0,
            "end_s": 2,
            "bbox": [0, 0, 100, 100],
            "expected_identity": "Jack",
        },
        {
            "id": "unknown",
            "start_s": 0,
            "end_s": 2,
            "bbox": [500, 0, 600, 100],
            "expected_identity": "unknown",
        },
    ]
    result = score_face_passages(
        [
            {"frame_time": 1, "bbox": [0, 0, 100, 100], "identity": "unknown"},
            {"frame_time": 1, "bbox": [500, 0, 600, 100], "identity": "Jack"},
        ],
        passages,
    )
    assert result["recall"] == 0
    assert result["precision"] == 0


def test_close_follow_rejects_old_generation_correlation() -> None:
    records = [
        {
            "camera": "face_camera",
            "round_id": 1,
            "track_id": "t",
            "generation": 1,
            "passage_id": "known",
        },
        {
            "camera": "face_camera",
            "round_id": 1,
            "track_id": "t",
            "generation": 1,
            "passage_id": "unknown",
        },
    ]
    # Fixture passage labels are a reporting view, not producer correlation.
    assert not correlation_mismatches(records)
    records[1]["generation"] = 2
    assert not correlation_mismatches(records)


def test_correlation_uses_phase6_lineage_without_removed_quality_fields() -> None:
    face = {
        "stage": "first_attempt",
        "pipeline": "face",
        "trace_id": "face:face_camera:raw-face",
        "camera": "face_camera",
        "track_id": "raw-face",
        "source_pts": 1.0,
        "person_box": [0, 0, 100, 100],
        "face_box": [20, 20, 50, 50],
    }
    lpr = {
        "stage": "event_published",
        "pipeline": "lpr",
        "trace_id": "lpr:car_camera:raw-lpr",
        "camera": "car_camera",
        "track_id": "raw-lpr",
        "source_pts": 2.0,
        "source_role": "detect",
        "object_box": [0, 0, 100, 100],
        "plate_box": [20, 20, 50, 50],
        "evidence_id": "evidence-1",
        "frame_ref": "evidence-1",
    }

    assert not correlation_mismatches([face, lpr])
    duplicate = correlation_mismatches([face, lpr, dict(lpr)])
    assert duplicate == [
        {
            "stage": "event_published",
            "reason": "duplicate_publication",
            "track_id": "raw-lpr",
            "source_pts": 2.0,
        }
    ]


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
                {
                    "passage_id": "known",
                    "round_id": round_id,
                    "stage": "first_qualified_face",
                    "trace_time": anchor + 0.1,
                    "frame_time": anchor + 0.1,
                },
                {
                    "passage_id": "known",
                    "round_id": round_id,
                    "stage": "first_attempt",
                    "identity": "Jack",
                    "trace_time": anchor + 0.2,
                    "frame_time": anchor + 0.2,
                },
                {
                    "passage_id": "known",
                    "round_id": round_id,
                    "stage": "confirmed_result",
                    "identity": "Jack",
                    "trace_time": anchor + 0.3,
                    "frame_time": anchor + 0.3,
                },
            ]
        )
    result, _, *_ = face_results(records, [passage], [100.0])
    assert result["passages"][0]["detected_rounds"] == 1
    assert result["passages"][0]["correct_rounds"] == 1
    assert result["detection_recall"] == 1
    assert result["accuracy"] == 1
    assert result["recall"] == 1


def test_face_without_fixture_passages_reports_raw_tracks() -> None:
    records = [
        {
            "pipeline": "face",
            "trace_id": "face:cam:raw-1",
            "stage": "track_seen",
        },
        {
            "pipeline": "face",
            "trace_id": "face:cam:raw-1",
            "stage": "first_attempt",
            "identity": "Jack",
        },
        {
            "pipeline": "face",
            "trace_id": "face:cam:raw-1",
            "stage": "confirmed_result",
            "identity": "Jack",
        },
        {
            "pipeline": "detector",
            "trace_id": "detector:cam:observation-1",
            "stage": "detector_hit",
        },
    ]

    result, false_records, *latencies = face_results(records, [], [100.0])

    assert result == {
        "mode": "raw_trace",
        "passages": [],
        "trace_count": 1,
        "track_seen_count": 1,
        "attempt_count": 1,
        "recognition_publish_count": 1,
        "recognition_completed_trace_count": 1,
        "recognition_coverage": 1.0,
        "recognized_unknown_trace_count": 0,
        "recognized_known_trace_count": 1,
        "not_recognized_trace_count": 0,
        "published_identities": ["Jack"],
        "accuracy": None,
        "detection_recall": None,
        "precision": None,
        "recall": None,
        "false_passages": None,
    }
    assert false_records == []
    assert latencies == [[], [], [], []]


def test_false_passage_count_deduplicates_stage_updates() -> None:
    records = [
        {
            "camera": "face_camera",
            "round_id": 1,
            "track_id": "t",
            "generation": 1,
            "stage": "first_attempt",
        },
        {
            "camera": "face_camera",
            "round_id": 1,
            "track_id": "t",
            "generation": 1,
            "stage": "confirmed_result",
        },
    ]
    assert false_passage_count(records) == 1


def test_unreadable_counts_recall_not_exact_denominator() -> None:
    passages = [
        {
            "id": "readable",
            "valid_passage": True,
            "readable": True,
            "expected_plate": "ABC123",
            "accepted_plates": [],
        },
        {
            "id": "unreadable",
            "valid_passage": True,
            "readable": False,
            "expected_plate": None,
            "accepted_plates": [],
        },
    ]
    records = []
    for round_id in range(1, 4):
        records += [
            {
                "stage": "event_published",
                "passage_id": "readable",
                "round_id": round_id,
                "plate": "ABC123",
                "score": 0.95,
            },
            {
                "stage": "event_published",
                "passage_id": "unreadable",
                "round_id": round_id,
                "plate": "NOISY",
                "score": 0.95,
            },
        ]
        for passage_id in ("readable", "unreadable"):
            records += [
                {"stage": stage, "passage_id": passage_id, "round_id": round_id}
                for stage in (
                    "detector_hit",
                    "track_seen",
                    "lpr_eligible",
                    "plate_detected",
                    "ocr_result",
                )
            ]
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
        {
            "id": "p",
            "valid_passage": True,
            "readable": True,
            "expected_plate": "ABC123",
            "accepted_plates": [],
        },
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


def test_lpr_exact_match_uses_terminal_pipeline_plate() -> None:
    passages = [
        {
            "id": "p",
            "valid_passage": True,
            "readable": True,
            "expected_plate": "ABC123",
            "accepted_plates": [],
        },
    ]
    records = []
    for round_id, plate in enumerate(("ABC123", "ABC123", "ABC12B"), start=1):
        records.extend(
            [
                {"stage": "track_seen", "passage_id": "p", "round_id": round_id},
                {
                    "stage": "event_published",
                    "passage_id": "p",
                    "round_id": round_id,
                    "plate": plate,
                    "score": 0.95,
                },
            ]
        )
    result, _ = lpr_results(records, passages)
    assert result["passages"][0]["representative"] == "ABC12B"
    assert result["passages"][0]["exact"] is False
    assert result["passages"][0]["consistency"] == pytest.approx(2 / 3)


def test_api_sqlite_consistency_reports_uncommitted_when_both_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [{"stage": "event_published", "track_id": "event-1", "plate": "ABC123"}]
    monkeypatch.setattr(passage_validator, "sqlite_events", lambda ids, path: {})
    monkeypatch.setattr(passage_validator, "api_event", lambda event_id: None)
    consistent, mismatches, uncommitted = api_sqlite_consistency(records, "unused.db")
    assert consistent
    assert mismatches == []
    assert uncommitted == [{"event_id": "event-1", "stage": "event_published"}]


def test_api_sqlite_consistency_rejects_one_sided_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [{"stage": "event_published", "track_id": "event-1", "plate": "ABC123"}]
    monkeypatch.setattr(
        passage_validator,
        "sqlite_events",
        lambda ids, path: {"event-1": {"data": {"recognized_license_plate": "ABC123"}}},
    )
    monkeypatch.setattr(passage_validator, "api_event", lambda event_id: None)
    times = iter((0.0, 3.0))
    monkeypatch.setattr(passage_validator.time, "monotonic", lambda: next(times))
    consistent, mismatches, uncommitted = api_sqlite_consistency(records, "unused.db")
    assert not consistent
    assert mismatches == [{"event_id": "event-1", "reason": "missing_api_or_sqlite"}]
    assert uncommitted == []


def test_plate_variants_are_normalized() -> None:
    assert normalize_plate("bee-3975") == "BEE3975"


def test_fixture_comparison_does_not_infer_a_missing_pipeline_stage() -> None:
    passage = {
        "id": "p",
        "valid_passage": True,
        "readable": False,
        "expected_plate": None,
        "accepted_plates": [],
    }
    records = []
    for round_id in range(1, 4):
        records += [
            {"stage": stage, "passage_id": "p", "round_id": round_id}
            for stage in ("detector_hit", "track_seen", "lpr_eligible")
        ]
    result, _ = lpr_results(records, [passage])
    assert result["passages"][0]["mismatch_reason"] is None


def test_recognition_lifecycle_summary_detects_attempts_duplicates_and_early_stop() -> (
    None
):
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
