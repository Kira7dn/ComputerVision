from pathlib import Path

import yaml

from bootstrap.config import (
    camera_ids,
    load_raw_config,
    resolve_camera_config,
)

ROOT = Path(__file__).parents[3]


def test_jetson_dev_launcher_uses_source_sync_and_vite_hmr() -> None:
    launcher = (ROOT / "app" / "deploy" / "powershell" / "start-jetson-dev.ps1").read_text(encoding="utf-8")
    assert "jetson_sync.py" in launcher
    assert "MediaMTX" not in launcher
    assert "Vite HMR" in launcher
    assert "ssh" in launcher
    assert "interfaces.mock_media_server" in launcher
    assert "http.server" not in launcher
    assert "start.ps1" not in launcher


def test_package_has_only_the_three_jetson_entrypoints() -> None:
    package = (ROOT / "package.json").read_text(encoding="utf-8")
    assert '"dev"' in package
    assert '"check"' in package
    assert '"deploy"' in package
    assert '"wsl:' not in package
    assert '"docker:' not in package


def test_cpu_analysis_branch_has_a_terminal_sink() -> None:
    pipeline = (ROOT / "app" / "src" / "application" / "camera_worker.py").read_text(
        encoding="utf-8"
    )

    assert 'make_element("fakesink", "analysis-sink")' in pipeline
    assert "analysis_src.link(analysis_sink)" in pipeline
    assert "self.fire_smoke_engine.last_inference_ran" in pipeline
    assert "self.fire_smoke_engine.last_fresh_detections" in pipeline
    assert "self.fire_smoke_events.visible_detections" in pipeline
    assert 'if raw_result["inference_ran"]:' in pipeline
    assert "fire_smoke_age_seconds" in pipeline
    assert "_analysis_max_age_frames" not in pipeline
    assert "detection_results = list(self._analysis_detections)" in pipeline
    assert "fire_smoke_detections = []" in pipeline


def test_live_output_drops_backlog_before_encoding() -> None:
    pipeline = (ROOT / "app" / "src" / "application" / "camera_worker.py").read_text(
        encoding="utf-8"
    )

    output_queue = pipeline.split('output_queue = make_element("queue", "output-queue")', 1)[1]
    assert 'output_queue.set_property("max-size-buffers", 2)' in output_queue
    assert 'output_queue.set_property("max-size-bytes", 0)' in output_queue
    assert 'output_queue.set_property("max-size-time", 0)' in output_queue
    assert 'output_queue.set_property("leaky", 2)' in output_queue


def test_dashboard_uses_webrtc_as_the_primary_live_transport() -> None:
    dashboard = (ROOT / "app" / "web" / "dashboard.html").read_text(encoding="utf-8")
    app = (ROOT / "app" / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    camera = (ROOT / "app" / "web" / "src" / "components" / "camera-card.tsx").read_text(encoding="utf-8")
    stream = (ROOT / "app" / "web" / "src" / "hooks" / "use-media-stream.ts").read_text(encoding="utf-8")

    assert '<script src="/mediamtx_reader.js"></script>' in dashboard
    assert '<script type="module" src="/src/main.tsx"></script>' in dashboard
    assert "<video ref={videoRef}" in camera
    assert "MediaMTXWebRTCReader" in stream
    assert "transport: 'hls-fallback'" in stream
    assert "autoPlay muted playsInline" in camera
    assert "LIVE_HARD_LATENCY_SECONDS" not in app


def test_mediamtx_reader_exposes_stats_and_dashboard_api_exposes_webrtc() -> None:
    reader = (ROOT / "app" / "web" / "mediamtx_reader.js").read_text(
        encoding="utf-8"
    )
    server = (ROOT / "app" / "src" / "interfaces" / "dashboard_api.py").read_text(
        encoding="utf-8"
    )

    assert "class MediaMTXWebRTCReader" in reader
    assert "getStats()" in reader
    assert "method: \"PATCH\"" in reader
    assert '"webrtc_url"' in server
    assert '"worker_ready"' in server
    assert '"hls_live"' not in server


def test_live_output_follows_rtsp_sample_contract() -> None:
    pipeline = (ROOT / "app" / "src" / "application" / "camera_worker.py").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "app" / "deploy" / "powershell" / "deploy-jetson.ps1").read_text(
        encoding="utf-8"
    )

    assert "nvstreammux" in pipeline
    assert 'make_element("nvdsosd", "bbox-osd")' in pipeline
    assert 'make_element("nvv4l2h264enc", "output-encoder")' in pipeline
    assert 'make_element("rtspclientsink", "rtsp-output")' in pipeline
    assert 'output_queue.set_property("leaky", 2)' in pipeline
    assert "deploy-jetson-native.ps1" in launcher
    assert "tbox_lab.ps1" not in launcher


def test_config_routes_functions_per_camera() -> None:
    config_path = ROOT / "app" / "config" / "production.yaml"
    raw = load_raw_config(config_path)

    assert camera_ids(raw) == ["DMS", "camera_front", "camera_back", "camera_left", "camera_right"]
    front = resolve_camera_config(raw, "camera_front")
    assert front["functions"]["front_assistance"] is True
    assert front["person"]["confidence"] == 0.05
    assert front["person"]["tracking"]["confirmation_hits"] == 2
    dahua = resolve_camera_config(raw, "DMS")
    assert dahua["input"]["mode"] == "rtsp"
    assert "channel=5" in dahua["input"]["rtsp_url"]
    assert dahua["functions"] == {
        "trace": False,
        "face_recognition": False,
        "smoking_behavior": False,
        "fire_smoke": False,
        "dms": True,
        "front_assistance": False,
    }


def test_config_is_valid_yaml_and_has_stable_evidence_contract() -> None:
    config_path = ROOT / "app" / "config" / "dev.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert isinstance(config["cameras"], list)
    assert config["runtime"]["analysis_result_max_age_seconds"] >= 1.0
    assert config["evidence"]["prefix"] == "snapshots-acceptance"
    assert config["evidence"]["snapshot_interval_ms"] >= 100
    assert config["fire_smoke"]["onnx_path"].endswith("best.onnx")
    assert config["smoking_behavior"]["onnx_path"].endswith(
        "smoking_behavior/model.onnx"
    )


def test_fire_smoke_canary_model_can_be_overridden_for_one_camera() -> None:
    raw = load_raw_config(ROOT / "app" / "config" / "production.yaml")
    raw["cameras"][1]["analysis"] = {
        "functions": {"fire_smoke": {"onnx_path": "/tmp/fire-smoke-candidate.onnx"}}
    }

    front = resolve_camera_config(raw, "camera_front")
    dahua = resolve_camera_config(raw, "DMS")

    assert front["fire_smoke"]["onnx_path"] == "/tmp/fire-smoke-candidate.onnx"
    assert dahua["fire_smoke"]["onnx_path"].endswith("fire_smoke/best.onnx")


def test_runtime_can_disable_notifications_for_acceptance(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSTREAM_NOTIFICATIONS_ENABLED", "false")

    resolved = resolve_camera_config(
        load_raw_config(ROOT / "app" / "config" / "production.yaml"),
        "DMS",
    )

    assert resolved["notifications"]["enabled"] is False


def test_event_feed_collapses_lifecycle_and_recognition_starts_on_exact_frame() -> None:
    dashboard_server = (ROOT / "app" / "src" / "interfaces" / "dashboard_api.py").read_text(
        encoding="utf-8"
    )
    pipeline = (ROOT / "app" / "src" / "application" / "camera_worker.py").read_text(
        encoding="utf-8"
    )
    notifications = (ROOT / "app" / "src" / "application" / "notification_service.py").read_text(
        encoding="utf-8"
    )

    assert 'if record_type not in {"START", "UPDATE", "END"}:' in dashboard_server
    assert "latest_by_event[event_id] = (sequence, record)" in dashboard_server
    assert '"thumbnail_url": thumbnail_url' in dashboard_server
    assert '"image_url": image_url' in dashboard_server
    assert "variant=thumbnail" in dashboard_server
    assert "max-age=31536000, immutable" in dashboard_server
    assert 'classification == "unrecognized"' in dashboard_server
    assert '"region_track_id": details.get("region_track_id")' in dashboard_server
    assert '"best_frame_number": details.get("best_frame_number")' in dashboard_server
    assert 'lifecycle != "START"' in notifications
    assert 'stable_name != "unknown"' in pipeline
    assert '"recognition_frame_number": recognition_frame' in pipeline
    assert 'self._notify_event(event_id, "START")' in pipeline
    assert 'self._notify_event(event_id, "END")' not in pipeline
    assert 'operation == "NOTIFY"' in pipeline
    assert 'operation == "START" and not hasattr(transition, "region_track_id")' not in pipeline


def test_event_items_open_a_full_detail_modal() -> None:
    panel = (ROOT / "app" / "web" / "src" / "components" / "event-panel.tsx").read_text(
        encoding="utf-8"
    )
    metadata = (ROOT / "app" / "web" / "src" / "components" / "event-detail-metadata.tsx").read_text(
        encoding="utf-8"
    )

    assert '<Dialog open={selected !== null}' in panel
    assert '<EventDetailMetadata event={selected} />' in panel
    assert 'JSON.stringify({ event, details: event.details' in metadata
    assert "event.best_frame_number" in metadata


def test_unknown_recognition_event_uses_best_frame_after_track_end() -> None:
    pipeline = (ROOT / "app" / "src" / "application" / "camera_worker.py").read_text(
        encoding="utf-8"
    )
    face_engine = (ROOT / "app" / "src" / "adapters" / "models" / "face_engine.py").read_text(
        encoding="utf-8"
    )

    assert "_track_best_evidence" in face_engine
    assert '"evidence_frame": best["frame_number"]' in face_engine
    assert "frame=best[\"frame\"] if best else None" in face_engine
    assert 'event_name == "track_end" and event_id is None' in pipeline
    assert 'f"face-unknown-{self.run_id}' in pipeline
    assert '"identity": "unknown"' in pipeline
    assert '"evidence_quality": data.get("evidence_quality")' in pipeline
