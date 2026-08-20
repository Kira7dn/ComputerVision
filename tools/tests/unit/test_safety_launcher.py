from pathlib import Path

import yaml

from deepstream_safety.config import (
    camera_ids,
    load_raw_config,
    resolve_camera_config,
)

ROOT = Path(__file__).parents[3]


def test_launcher_uses_the_current_deepstream_runtime() -> None:
    launcher = (ROOT / "deepstream_safety" / "start.ps1").read_text(encoding="utf-8")
    assert "multi_runner.py" in launcher
    assert "dashboard_server.py" in launcher
    assert "deploy/run.ps1" not in launcher
    assert "docker" not in launcher.lower()


def test_cpu_analysis_branch_has_a_terminal_sink() -> None:
    pipeline = (ROOT / "deepstream_safety" / "pipeline.py").read_text(
        encoding="utf-8"
    )

    assert 'make_element("fakesink", "analysis-sink")' in pipeline
    assert "analysis_src.link(analysis_sink)" in pipeline
    assert "self.fire_smoke_engine.last_inference_ran" in pipeline
    assert "self.fire_smoke_engine.last_fresh_detections" in pipeline
    assert "analysis_age_seconds" in pipeline
    assert "_analysis_max_age_frames" not in pipeline
    assert "detection_results = list(self._analysis_detections)" in pipeline
    assert "fire_smoke_detections = []" in pipeline


def test_live_output_drops_backlog_before_encoding() -> None:
    pipeline = (ROOT / "deepstream_safety" / "pipeline.py").read_text(
        encoding="utf-8"
    )

    output_queue = pipeline.split('output_queue = make_element("queue", "output-queue")', 1)[1]
    assert 'output_queue.set_property("max-size-buffers", 2)' in output_queue
    assert 'output_queue.set_property("max-size-bytes", 0)' in output_queue
    assert 'output_queue.set_property("max-size-time", 0)' in output_queue
    assert 'output_queue.set_property("leaky", 2)' in output_queue


def test_dashboard_uses_webrtc_as_the_primary_live_transport() -> None:
    dashboard = (ROOT / "deepstream_safety" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert '<video id="video-${camera.id}" hidden autoplay muted playsinline></video>' in dashboard
    assert " controls autoplay" not in dashboard
    assert '<script src="/mediamtx_reader.js"></script>' in dashboard
    assert "MediaMTXWebRTCReader" in dashboard
    assert "startWebRtc(video, camera.webrtc_url, camera.hls_url, state)" in dashboard
    assert "startHlsFallback" in dashboard
    assert "startHls(video" not in dashboard
    assert "LIVE_HARD_LATENCY_SECONDS" not in dashboard


def test_mediamtx_reader_exposes_stats_and_dashboard_api_exposes_webrtc() -> None:
    reader = (ROOT / "deepstream_safety" / "mediamtx_reader.js").read_text(
        encoding="utf-8"
    )
    server = (ROOT / "deepstream_safety" / "dashboard_server.py").read_text(
        encoding="utf-8"
    )

    assert "class MediaMTXWebRTCReader" in reader
    assert "getStats()" in reader
    assert "method: \"PATCH\"" in reader
    assert '"webrtc_url"' in server
    assert '"worker_ready"' in server
    assert '"hls_live"' not in server


def test_live_output_follows_rtsp_sample_contract() -> None:
    pipeline = (ROOT / "deepstream_safety" / "pipeline.py").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "deepstream_safety" / "start.ps1").read_text(
        encoding="utf-8"
    )

    assert "nvstreammux" in pipeline
    assert 'make_element("nvdsosd", "bbox-osd")' in pipeline
    assert 'make_element("nvv4l2h264enc", "output-encoder")' in pipeline
    assert 'make_element("rtspclientsink", "rtsp-output")' in pipeline
    assert 'output_queue.set_property("leaky", 2)' in pipeline
    assert "NVDS_ENABLE_LATENCY_MEASUREMENT=1" in launcher
    assert "NVDS_ENABLE_COMPONENT_LATENCY_MEASUREMENT=1" in launcher


def test_config_routes_functions_per_camera() -> None:
    config_path = ROOT / "deepstream_safety" / "config.yaml"
    raw = load_raw_config(config_path)

    assert camera_ids(raw) == ["camera_face", "camera_safety", "camera_dahua"]
    face = resolve_camera_config(raw, "camera_face")
    safety = resolve_camera_config(raw, "camera_safety")

    assert face["functions"] == {
        "trace": True,
        "face_recognition": True,
        "smoking_behavior": False,
        "fire_smoke": False,
    }
    assert safety["functions"] == {
        "trace": True,
        "face_recognition": False,
        "smoking_behavior": True,
        "fire_smoke": True,
    }
    dahua = resolve_camera_config(raw, "camera_dahua")
    assert "channel=5" in dahua["input"]["rtsp_url"]
    assert dahua["functions"] == {
        "trace": True,
        "face_recognition": False,
        "smoking_behavior": True,
        "fire_smoke": True,
    }


def test_config_is_valid_yaml_and_has_stable_evidence_contract() -> None:
    config_path = ROOT / "deepstream_safety" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert isinstance(config["cameras"], list)
    assert config["runtime"]["analysis_result_max_age_seconds"] >= 1.0
    assert config["evidence"]["prefix"] == "snapshots-acceptance"
    assert config["evidence"]["snapshot_interval_ms"] >= 100
    assert config["fire_smoke"]["onnx_path"].endswith("best.onnx")
    assert config["smoking_behavior"]["onnx_path"].endswith(
        "smoking_behavior/model.onnx"
    )


def test_runtime_can_disable_notifications_for_acceptance(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSTREAM_NOTIFICATIONS_ENABLED", "false")

    resolved = resolve_camera_config(
        load_raw_config(ROOT / "deepstream_safety" / "config.yaml"),
        "camera_safety",
    )

    assert resolved["notifications"]["enabled"] is False


def test_event_feed_is_start_only_and_recognition_starts_on_exact_frame() -> None:
    dashboard_server = (ROOT / "deepstream_safety" / "dashboard_server.py").read_text(
        encoding="utf-8"
    )
    pipeline = (ROOT / "deepstream_safety" / "pipeline.py").read_text(
        encoding="utf-8"
    )
    notifications = (ROOT / "deepstream_safety" / "notifications.py").read_text(
        encoding="utf-8"
    )

    assert 'if record_type != "START":' in dashboard_server
    assert '"thumbnail_url": thumbnail_url' in dashboard_server
    assert 'lifecycle != "START"' in notifications
    assert 'stable_name != "unknown"' in pipeline
    assert '"recognition_frame_number": recognition_frame' in pipeline
    assert 'self._notify_event(event_id, "START")' in pipeline
    assert 'self._notify_event(event_id, "END")' not in pipeline


def test_event_items_open_a_full_detail_modal() -> None:
    dashboard = (ROOT / "deepstream_safety" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'id="eventModal"' in dashboard
    assert 'function openEventModal(event)' in dashboard
    assert 'eventModal.addEventListener' in dashboard
    assert 'JSON.stringify(modalData, null, 2)' in dashboard
    assert 'event.details?.recognition_frame_number' in dashboard
