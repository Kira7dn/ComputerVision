import numpy as np
import pytest

from deepstream_safety.config import camera_ids, resolve_camera_config
from deepstream_safety.face_engine import (
    TrackRecognitionScheduler,
    _select_onnx_providers,
)


def test_track_scheduler_uses_bounded_wall_clock_interval() -> None:
    current = [10.0]
    scheduler = TrackRecognitionScheduler(100, clock=lambda: current[0])

    assert scheduler.interval_ms == 300.0
    assert scheduler.due(7, current[0])
    scheduler.mark_attempt(7, current[0])
    assert not scheduler.due(7, 10.299)
    assert scheduler.due(7, 10.3)

    current[0] = 10.4
    scheduler.mark_attempt(8)
    assert scheduler.due(7, current[0])
    assert not scheduler.due(8, 10.699)
    scheduler.forget(8)
    assert scheduler.due(8, 10.4)


def test_track_scheduler_clamps_upper_bound() -> None:
    scheduler = TrackRecognitionScheduler(2000)

    assert scheduler.interval_ms == 500.0
    assert scheduler.interval_seconds == 0.5


def test_unknown_evidence_keeps_only_the_highest_quality_frame() -> None:
    from deepstream_safety.face_engine import FaceRecognitionEngine

    engine = FaceRecognitionEngine.__new__(FaceRecognitionEngine)
    engine._track_best_evidence = {}
    engine.detector_threshold = 0.5
    flat = np.full((48, 64, 3), 128, dtype=np.uint8)
    sharp = np.indices((48, 64)).sum(axis=0).astype(np.uint8) * 120
    sharp = np.repeat(sharp[:, :, None], 3, axis=2)

    engine._consider_best_evidence(7, flat, 10, [0, 0, 64, 48], [8, 8, 20, 20], 0.70)
    engine._consider_best_evidence(7, sharp, 20, [0, 0, 64, 48], [8, 8, 20, 20], 0.90)

    candidate = engine._track_best_evidence[7]
    assert candidate["frame_number"] == 20
    assert candidate["face_score"] == 0.90
    assert candidate["quality"] > 0.0


def test_unknown_evidence_rejects_person_only_frame() -> None:
    from deepstream_safety.face_engine import FaceRecognitionEngine

    engine = FaceRecognitionEngine.__new__(FaceRecognitionEngine)
    engine._track_best_evidence = {}
    engine.detector_threshold = 0.5
    frame = np.full((48, 64, 3), 128, dtype=np.uint8)

    engine._consider_best_evidence(7, frame, 10, [0, 0, 64, 48])

    assert engine._track_best_evidence == {}


def test_provider_selection_prefers_available_gpu_and_keeps_cpu_fallback() -> None:
    selected = _select_onnx_providers(
        ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert selected == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_provider_selection_falls_back_to_cpu_when_gpu_is_unavailable() -> None:
    selected = _select_onnx_providers(
        ["TensorrtExecutionProvider", "CUDAExecutionProvider"],
        ["CPUExecutionProvider"],
    )

    assert selected == ["CPUExecutionProvider"]


def test_multi_camera_config_isolated_per_camera() -> None:
    config = {
        "smoking_behavior": {"onnx_path": "/models/smoking_behavior/model.onnx"},
        "recognition": {"enabled": True, "face_runtime": {"trace_directory": "/trace"}},
        "snapshots": {"directory": "/snapshots"},
        "events": {"directory": "/events"},
        "cameras": [
            {
                "id": "face_camera",
                "source": {"type": "rtsp", "url": "rtsp://10.0.0.1/face"},
                "output": {"rtsp_url": "rtsp://127.0.0.1:8554/face_bbox"},
                "functions": {
                    "trace": True,
                    "face_recognition": True,
                    "smoking_behavior": False,
                },
            },
            {
                "id": "safety_camera",
                "source": {
                    "type": "mock",
                    "url": "rtsp://127.0.0.1:8554/safety_mock",
                    "mock_video": "/tmp/safety.mp4",
                },
                "output": {"rtsp_url": "rtsp://127.0.0.1:8554/safety_bbox"},
                "functions": {
                    "trace": False,
                    "face_recognition": False,
                    "smoking_behavior": True,
                },
            },
        ],
    }

    assert camera_ids(config) == ["face_camera", "safety_camera"]
    face = resolve_camera_config(config, "face_camera")
    safety = resolve_camera_config(config, "safety_camera")

    assert face["input"]["camera"] == "face_camera"
    assert face["input"]["mode"] == "rtsp"
    assert face["recognition"]["enabled"] is True
    assert face["smoking_behavior"]["enabled"] is False
    assert face["events"]["enabled"] is False
    assert "directory" not in face["events"]
    assert "directory" not in face["snapshots"]
    assert face["metadata"]["zmq_pub_url"] == "tcp://127.0.0.1:5555"
    assert safety["input"]["mode"] == "mock"
    assert safety["recognition"]["enabled"] is False
    assert safety["smoking_behavior"]["enabled"] is True
    assert safety["recognition"]["face_runtime"]["trace_enabled"] is False
    assert safety["events"]["trace_enabled"] is False
    assert safety["metadata"]["zmq_pub_url"] == "tcp://127.0.0.1:5556"


def test_multi_camera_config_requires_camera_id() -> None:
    with pytest.raises(ValueError, match="camera_id is required"):
        resolve_camera_config({"cameras": [{"id": "a"}, {"id": "b"}]})
