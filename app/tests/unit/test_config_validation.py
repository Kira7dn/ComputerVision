from pathlib import Path

import pytest

from bootstrap.config import load_raw_config, resolve_camera_config, validate_config


def test_profiles_merge_and_production_has_no_mock_source() -> None:
    dev = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    production = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")

    assert dev["profile"] == "dev"
    assert [camera["id"] for camera in dev["cameras"]] == [
        "DMS", "camera_front", "camera_back", "camera_left", "camera_right"
    ]
    assert all(camera["source"]["type"] == "mock" for camera in dev["cameras"][1:])
    assert dev["cameras"][1]["source"].get("media_only", False) is False
    assert all(camera["source"]["media_only"] for camera in dev["cameras"][2:])
    assert {camera["source"]["sync_period_seconds"] for camera in dev["cameras"][1:]} == {191.1}
    assert all(camera["source"]["type"] == "rtsp" for camera in production["cameras"])


def test_runtime_directories_can_be_overridden_after_config_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.setenv("CAMERA_EVIDENCE_DIR", "/opt/ls-vision-dev/data/evidence")
    monkeypatch.setenv("CAMERA_STATE_DIR", "/opt/ls-vision-dev/data/state")
    monkeypatch.setenv("CAMERA_STATUS_DIR", "/opt/ls-vision-dev/data/status")

    config = load_raw_config(root / "config" / "production-jetson-native.yaml")

    assert config["evidence"]["directory"] == "/opt/ls-vision-dev/data/evidence"
    assert config["runtime"]["state_directory"] == "/opt/ls-vision-dev/data/state"
    assert config["runtime"]["status_directory"] == "/opt/ls-vision-dev/data/status"


def test_production_mock_source_is_rejected() -> None:
    config = {
        "profile": "production",
        "cameras": [{"id": "camera", "source": {"type": "mock", "url": "rtsp://x/y"}, "output": {"rtsp_url": "rtsp://x/z"}}],
    }
    with pytest.raises(ValueError, match="cannot use mock"):
        validate_config(config)


def test_front_mock_has_worker_and_calibration_contract() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    resolved = resolve_camera_config(raw, "camera_front")

    assert resolved["input"]["media_only"] is False
    assert resolved["input"]["mock_sync_group"] == "vehicle_surround"
    assert resolved["input"]["mock_sync_period_seconds"] == 191.1
    assert resolved["functions"]["front_assistance"] is True
    assert resolved["front_assistance"]["enabled"] is True
    assert resolved["front_assistance"]["calibration"]["source_width"] == 960
    assert resolved["input"]["rtsp_url"].endswith("/camera_front_raw")
    assert resolved["output"]["rtsp_url"].endswith("/camera_front")


def test_media_only_mock_rejects_enabled_cv_function() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_back")
    camera["functions"]["fire_smoke"] = True

    with pytest.raises(ValueError, match="media_only cannot enable functions"):
        validate_config(raw)


def test_duplicate_camera_id_is_rejected() -> None:
    config = {
        "cameras": [
            {"id": "same", "source": {"url": "rtsp://x/a"}, "output": {"rtsp_url": "rtsp://x/b"}},
            {"id": "same", "source": {"url": "rtsp://x/c"}, "output": {"rtsp_url": "rtsp://x/d"}},
        ]
    }
    with pytest.raises(ValueError, match="duplicate camera id"):
        validate_config(config)


def test_analysis_overrides_are_isolated_per_camera() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")

    safety = resolve_camera_config(raw, "camera_safety")
    dahua = resolve_camera_config(raw, "DMS")

    assert safety["runtime"]["analysis_result_max_age_seconds"] == 1.0
    assert safety["fire_smoke"]["fire_threshold"] == 0.30
    assert safety["fire_smoke"]["smoke_threshold"] == 0.40
    assert safety["fire_smoke"]["class_rois"]["fire"] == [0.30, 0.05, 0.70, 0.80]
    assert dahua["fire_smoke"]["class_rois"] == {}
    assert dahua["fire_smoke"]["fire_threshold"] == 0.30
    assert dahua["fire_smoke"]["tracking"]["confirmation_hits"] == 4
    assert dahua["fire_smoke"]["tracking"]["confirmation_window"] == 6
    assert dahua["fire_smoke"]["dynamics"]["mode"] == "advisory"
    assert dahua["fire_smoke"]["tracking"]["notification_min_duration_seconds"] == 3.0
    assert dahua["functions"]["dms"] is True
    assert dahua["functions"]["smoking_behavior"] is False
    assert dahua["functions"]["fire_smoke"] is False
    dms_models = dahua["dms"]["object_detection"]["models"]
    assert set(dms_models["chaitanya"]["positive_labels"]) == {
        "Cigarette", "Drinking", "Eating", "Phone", "Seatbelt"
    }
    assert dahua["dms"]["alerts"] == {"on_frames": 3, "off_frames": 2}
    assert dahua["dms"]["event_policy"]["model"] == {
        "min_score": 0.50,
        "require_person_match": True,
        "confirmation_hits": 6,
        "confirmation_window": 10,
        "minimum_duration_seconds": 1.0,
        "candidate_timeout_seconds": 3.0,
        "clear_seconds": 2.0,
        "unknown_timeout_seconds": 1.5,
        "trace_interval_ms": 1000,
    }
    assert dahua["dms"]["event_policy"]["no_seatbelt"]["confirmation_hits"] == 12
    assert safety["smoking_behavior"]["padding_ratio"] == 0.20
    assert safety["smoking_behavior"]["temporal"]["confirmation_hits"] == 2
    assert safety["smoking_behavior"]["temporal"]["confirmation_window"] == 4
    assert safety["smoking_behavior"]["temporal"]["minimum_duration_seconds"] == 0.4
    assert safety["smoking_behavior"]["lifecycle"]["clearing_seconds"] == 3.0
    object_models = safety["smoking_behavior"]["object_detection"]["models"]
    assert object_models["chaitanya"]["positive_labels"] == ["Cigarette"]
    assert object_models["soham"]["positive_labels"] == ["Smoking"]
    assert "events" not in safety


def test_invalid_camera_analysis_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_safety")
    camera["analysis"]["functions"]["smoking"]["crop"]["strategy"] = "upper_body"

    with pytest.raises(ValueError, match="crop.strategy"):
        resolve_camera_config(raw, "camera_safety")


def test_invalid_smoking_temporal_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_safety")
    camera["analysis"]["functions"]["smoking"]["confirmation"] = {
        "hits": 5,
        "attempts": 4,
    }

    with pytest.raises(ValueError, match="smoking confirmation"):
        resolve_camera_config(raw, "camera_safety")


def test_invalid_dms_confirmation_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    raw["dms"]["event_policy"]["model"]["confirmation_hits"] = 11
    raw["dms"]["event_policy"]["model"]["confirmation_window"] = 10

    with pytest.raises(ValueError, match="dms.event_policy.model confirmation window"):
        validate_config(raw)


def test_smoking_notification_delay_cannot_precede_confirmation_duration() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_safety")
    camera["analysis"]["functions"]["smoking"]["temporal"] = {
        "minimum_duration_seconds": 1.0,
    }
    camera["analysis"]["functions"]["smoking"]["lifecycle"] = {
        "notification_min_duration_seconds": 0.5,
    }

    with pytest.raises(ValueError, match="notification_min_duration_seconds"):
        resolve_camera_config(raw, "camera_safety")


def test_all_profiles_keep_dynamics_in_advisory_mode() -> None:
    root = Path(__file__).parents[2] / "config"
    production = resolve_camera_config(
        load_raw_config(root / "production.yaml"), "DMS"
    )
    e2e = resolve_camera_config(load_raw_config(root / "e2e.yaml"), "DMS")

    assert production["fire_smoke"]["dynamics"]["mode"] == "advisory"
    assert e2e["fire_smoke"]["dynamics"]["mode"] == "advisory"


def test_invalid_fire_dynamics_vote_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"]["functions"]["fire_smoke"]["dynamics"] = {
        "confirmation_votes": 6,
        "confirmation_window": 5,
    }

    with pytest.raises(ValueError, match="confirmation_votes"):
        resolve_camera_config(raw, "DMS")


def test_fire_dynamics_hard_enforcement_is_rejected() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"]["functions"]["fire_smoke"]["dynamics"] = {
        "enforce": True
    }

    with pytest.raises(ValueError, match="hard enforcement is unsupported"):
        resolve_camera_config(raw, "DMS")
