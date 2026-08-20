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
