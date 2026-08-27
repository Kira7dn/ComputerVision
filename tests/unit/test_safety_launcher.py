from pathlib import Path

import yaml

from bootstrap.config import (
    camera_ids,
    load_raw_config,
    resolve_camera_config,
)

ROOT = Path(__file__).parents[2]
APP_ROOT = ROOT / "apps"


def test_jetson_dev_launcher_uses_source_sync_and_vite_hmr() -> None:
    launcher = (ROOT / "deploy" / "powershell" / "deploy-jetson-dev.ps1").read_text(encoding="utf-8")
    assert "jetson_sync.py" in launcher
    assert "vite.js" in launcher
    assert "ssh" in launcher
    assert "interfaces.mock_media_server" in launcher
    assert "http.server" not in launcher
    assert "start.ps1" not in launcher
    assert "18080:127.0.0.1:28080" in launcher
    assert "8888:127.0.0.1:28888" in launcher
    assert "8889:127.0.0.1:28889" in launcher


def test_package_has_only_the_supported_jetson_entrypoints() -> None:
    package = (ROOT / "package.json").read_text(encoding="utf-8")
    assert '"dev"' in package
    assert '"test"' in package
    assert '"deploy"' in package


def test_cpu_analysis_branch_has_a_terminal_sink() -> None:
    pipeline = (APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py").read_text(
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
    pipeline = (APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py").read_text(
        encoding="utf-8"
    )

    output_queue = pipeline.split('output_queue = make_element("queue", "output-queue")', 1)[1]
    assert 'output_queue.set_property("max-size-buffers", 2)' in output_queue
    assert 'output_queue.set_property("max-size-bytes", 0)' in output_queue
    assert 'output_queue.set_property("max-size-time", 0)' in output_queue
    assert 'output_queue.set_property("leaky", 2)' in output_queue


def test_dashboard_uses_webrtc_as_the_primary_live_transport() -> None:
    dashboard = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
    app = (APP_ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    camera = (APP_ROOT / "web" / "src" / "components" / "camera-card.tsx").read_text(encoding="utf-8")
    stream = (APP_ROOT / "web" / "src" / "hooks" / "use-media-stream.ts").read_text(encoding="utf-8")

    assert '<script src="/mediamtx_reader.js"></script>' in dashboard
    assert '<script type="module" src="/src/main.tsx"></script>' in dashboard
    assert "<video ref={videoRef}" in camera
    assert "MediaMTXWebRTCReader" in stream
    assert "transport: 'hls-fallback'" in stream
    assert "autoPlay muted playsInline" in camera
    assert "LIVE_HARD_LATENCY_SECONDS" not in app


def test_mediamtx_reader_exposes_stats_and_dashboard_api_exposes_webrtc() -> None:
    reader = (APP_ROOT / "web" / "mediamtx_reader.js").read_text(
        encoding="utf-8"
    )
    server = (APP_ROOT / "src" / "interfaces" / "dashboard_api.py").read_text(
        encoding="utf-8"
    )

    assert "class MediaMTXWebRTCReader" in reader
    assert "getStats()" in reader
    assert "method: \"PATCH\"" in reader
    assert '"webrtc_url"' in server
    assert '"worker_ready"' in server
    assert '"hls_live"' not in server


def test_live_output_follows_rtsp_sample_contract() -> None:
    pipeline = (APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py").read_text(
        encoding="utf-8"
    )
    launcher = (ROOT / "deploy" / "powershell" / "deploy-jetson.ps1").read_text(
        encoding="utf-8"
    )

    assert "nvstreammux" in pipeline
    assert 'make_element("nvdsosd", "bbox-osd")' in pipeline
    assert 'make_element("nvv4l2h264enc", "output-encoder")' in pipeline
    assert 'make_element("rtspclientsink", "rtsp-output")' in pipeline
    assert 'output_queue.set_property("leaky", 2)' in pipeline
    assert "deploy-jetson-dev.ps1" in launcher
    assert "tbox_lab.ps1" not in launcher


def test_optional_input_mirror_runs_before_nvstreammux() -> None:
    pipeline = (APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py").read_text(
        encoding="utf-8"
    )

    mirror = pipeline.index('input_transform.set_property("flip-method", 4)')
    streammux = pipeline.index('make_element("nvstreammux", "stream-muxer")')
    inference = pipeline.index('make_element("nvinfer", "person-inference")')

    assert mirror < streammux < inference
    assert '"input_mirror_horizontal"' in pipeline


def test_synchronized_mock_timeline_is_owned_outside_camera_worker() -> None:
    timeline = (
        APP_ROOT / "src" / "application" / "mock_timeline_runtime.py"
    ).read_text(encoding="utf-8")
    pipeline = (
        APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py"
    ).read_text(encoding="utf-8")
    service = (APP_ROOT / "src" / "service.py").read_text(encoding="utf-8")

    assert '"application.mock_timeline_runtime"' in service
    assert '"adapters.media.gstreamer_mock_publisher"' in timeline
    synchronized_branch = pipeline.split("if synchronized:", 1)[1].split(
        'if shutil.which("ffmpeg"):', 1
    )[0]
    assert "wait_for_rtsp_video" in synchronized_branch
    assert "subprocess.Popen" not in synchronized_branch

    browser_sync = (APP_ROOT / "web" / "src" / "lib" / "mock-stream-sync.ts").read_text(
        encoding="utf-8"
    )
    assert "__LS_VISION_SYNC_STATUS__" in browser_sync
    assert "const STABLE_LOCK_SECONDS = 2" in browser_sync
    assert "const MAX_DRIFT_MS = 250" in browser_sync


def test_jetson_dev_service_is_isolated_from_production() -> None:
    service = (ROOT / "deploy" / "systemd" / "ls-vision-dev.service").read_text(
        encoding="utf-8"
    )

    assert "Conflicts=ls-vision.service" not in service
    assert "CAMERA_CONFIG=/opt/ls-vision-dev/current/app/config/dev.yaml" in service
    assert "CAMERA_DASHBOARD_PORT=28080" in service
    assert "CAMERA_INPUT_RTSP_BASE=rtsp://127.0.0.1:28554" in service
    assert "CAMERA_OUTPUT_RTSP_BASE=rtsp://127.0.0.1:28554" in service
    assert "MTX_HLSADDRESS=:28888" in service
    assert "MTX_WEBRTCADDRESS=:28889" in service

    launcher = (ROOT / "deploy" / "powershell" / "deploy-jetson-dev.ps1").read_text(
        encoding="utf-8"
    )
    assert "systemctl enable --now ls-vision-dev.service" not in launcher
    assert "systemctl disable --now ls-vision-dev.service" in launcher


def test_vision_ingress_has_no_tbox_service_dependency() -> None:
    ingress = (ROOT / "deploy" / "systemd" / "ls-vision-ingress.service").read_text(
        encoding="utf-8"
    )

    assert "tbox.service" not in ingress
    assert "After=network-online.target" in ingress
    assert "Wants=network-online.target" in ingress


def test_config_routes_functions_per_camera() -> None:
    config_path = ROOT / "config" / "production.yaml"
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
    config_path = ROOT / "config" / "dev.yaml"
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
    raw = load_raw_config(ROOT / "config" / "production.yaml")
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
        load_raw_config(ROOT / "config" / "production.yaml"),
        "DMS",
    )

    assert resolved["notifications"]["enabled"] is False


def test_event_feed_collapses_lifecycle_and_recognition_starts_on_exact_frame() -> None:
    dashboard_server = (APP_ROOT / "src" / "interfaces" / "dashboard_api.py").read_text(
        encoding="utf-8"
    )
    pipeline = (APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py").read_text(
        encoding="utf-8"
    )
    notifications = (APP_ROOT / "src" / "adapters" / "notification" / "service.py").read_text(
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
    panel = (APP_ROOT / "web" / "src" / "components" / "event-panel.tsx").read_text(
        encoding="utf-8"
    )
    metadata = (APP_ROOT / "web" / "src" / "components" / "event-detail-metadata.tsx").read_text(
        encoding="utf-8"
    )

    assert '<Dialog open={selected !== null}' in panel
    assert '<EventDetailMetadata event={selected} />' in panel
    assert 'JSON.stringify({ event, details: event.details' in metadata
    assert "event.best_frame_number" in metadata


def test_unknown_recognition_event_uses_best_frame_after_track_end() -> None:
    pipeline = (APP_ROOT / "src" / "adapters" / "deepstream" / "runtime.py").read_text(
        encoding="utf-8"
    )
    face_engine = (APP_ROOT / "src" / "adapters" / "models" / "face_engine.py").read_text(
        encoding="utf-8"
    )

    assert "_track_best_evidence" in face_engine
    assert '"evidence_frame": best["frame_number"]' in face_engine
    assert "frame=best[\"frame\"] if best else None" in face_engine
    assert 'event_name == "track_end" and event_id is None' in pipeline
    assert 'f"face-unknown-{self.run_id}' in pipeline
    assert '"identity": "unknown"' in pipeline
    assert '"evidence_quality": data.get("evidence_quality")' in pipeline
