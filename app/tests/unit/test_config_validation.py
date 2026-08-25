from pathlib import Path

import pytest

from bootstrap.config import load_raw_config, resolve_camera_config, validate_config


def test_dev_and_production_share_the_same_five_camera_topology() -> None:
    dev = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    production = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    expected_ids = ["DMS", "camera_front", "camera_back", "camera_left", "camera_right"]

    assert dev["profile"] == "dev"
    assert [camera["id"] for camera in dev["cameras"]] == expected_ids
    assert [camera["id"] for camera in production["cameras"]] == expected_ids
    assert dev["cameras"][0]["source"]["type"] == "rtsp"
    assert production["cameras"][0]["source"]["type"] == "rtsp"
    assert all(camera["source"]["type"] == "mock" for camera in dev["cameras"][1:])
    assert all(
        camera["source"]["type"] == "mock" for camera in production["cameras"][1:]
    )
    assert [Path(camera["source"]["mock_video"]).name for camera in dev["cameras"][1:]] == [
        Path(camera["source"]["mock_video"]).name
        for camera in production["cameras"][1:]
    ]
    assert {
        camera_id: resolve_camera_config(production, camera_id)["functions"]
        for camera_id in expected_ids
    } == {
        camera_id: resolve_camera_config(dev, camera_id)["functions"]
        for camera_id in expected_ids
    }
    for profile in (dev, production):
        assert "channel=5" in profile["cameras"][0]["source"]["url"]
        assert all(camera["source"].get("mock_video") for camera in profile["cameras"][1:])
        surround = profile["cameras"][1:]
        assert {camera["source"]["sync_group"] for camera in surround} == {
            "vehicle_surround"
        }
        assert {camera["source"]["sync_period_seconds"] for camera in surround} == {
            191.1
        }
        assert {camera["source"]["sync_epoch_seconds"] for camera in surround} == {
            0.0
        }
        assert "mode" not in profile["dms"]["attention"]
        assert "model_path" not in profile["dms"]["attention"]


def test_runtime_directories_can_be_overridden_after_config_inheritance(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.setenv("CAMERA_EVIDENCE_DIR", "/opt/ls-vision-dev/data/evidence")
    monkeypatch.setenv("CAMERA_STATE_DIR", "/opt/ls-vision-dev/data/state")
    monkeypatch.setenv("CAMERA_STATUS_DIR", "/opt/ls-vision-dev/data/status")

    config = load_raw_config(root / "config" / "production.yaml")

    assert config["evidence"]["directory"] == "/opt/ls-vision-dev/data/evidence"
    assert config["runtime"]["state_directory"] == "/opt/ls-vision-dev/data/state"
    assert config["runtime"]["status_directory"] == "/opt/ls-vision-dev/data/status"


def test_output_rtsp_base_can_be_overridden_without_changing_stream_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.setenv("CAMERA_OUTPUT_RTSP_BASE", "rtsp://mediamtx:8554")

    resolved = resolve_camera_config(load_raw_config(root / "config" / "production.yaml"), "camera_front")

    assert resolved["output"]["rtsp_url"] == "rtsp://mediamtx:8554/camera_front"


def test_production_enables_bounded_dms_performance_contract() -> None:
    root = Path(__file__).parents[2]
    raw = load_raw_config(root / "config" / "production.yaml")
    resolved = resolve_camera_config(raw, "DMS")

    assert resolved["person"]["inference_interval"] == 1
    assert resolved["person"]["roi_cache_max_age_seconds"] == 0.5
    assert resolved["runtime"]["live_metadata_interval_ms"] == 250
    assert resolved["dms"]["face_mesh"]["driver_roi"] == {
        "enabled": True,
        "require_person": True,
        "upper_body_ratio": 0.55,
        "padding_ratio": 0.10,
        "max_side": 640,
    }
    assert resolved["dms"]["event_policy"]["model"]["trace_interval_ms"] == 2000


def test_production_mock_sources_are_supported() -> None:
    config = {
        "profile": "production",
        "cameras": [
            {
                "id": "DMS",
                "source": {
                    "type": "mock",
                    "url": "rtsp://127.0.0.1:8554/dms_raw",
                    "mock_video": "/opt/ls-vision/data/mock-videos/dms/bucket11.mp4",
                },
                "output": {"rtsp_url": "rtsp://127.0.0.1:8554/dahua_bbox"},
            }
        ],
    }

    assert validate_config(config)["profile"] == "production"


def test_front_camera_has_worker_and_calibration_contract() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    resolved = resolve_camera_config(raw, "camera_front")

    assert resolved["input"]["media_only"] is False
    assert resolved["input"]["mode"] == "mock"
    assert resolved["input"]["mock_sync_group"] == "vehicle_surround"
    assert resolved["input"]["mock_sync_period_seconds"] == 191.1
    assert resolved["input"]["mock_sync_epoch_seconds"] == 0.0
    assert resolved["functions"]["front_assistance"] is True
    assert resolved["front_assistance"]["enabled"] is True
    assert resolved["front_assistance"]["traffic_convention"] == "right_hand"
    assert resolved["front_assistance"]["overlay"] == {
        "lane_min_probability": 0.5,
        "path_half_width_m": 0.9,
    }
    assert resolved["front_assistance"]["calibration"]["source_width"] == 960
    assert resolved["input"]["mock_video"].endswith("/CAM_FRONT.mp4")
    assert resolved["input"]["rtsp_url"].endswith("/camera_front_raw")
    assert resolved["output"]["rtsp_url"].endswith("/camera_front")


def test_media_only_mock_rejects_enabled_cv_function() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_back")
    camera["source"].update(
        {
            "type": "mock",
            "media_only": True,
            "sync_group": "vehicle_surround",
            "sync_period_seconds": 191.1,
            "sync_epoch_seconds": 0.0,
        }
    )
    camera["functions"]["fire_smoke"] = True

    with pytest.raises(ValueError, match="media_only cannot enable functions"):
        validate_config(raw)


def test_synchronized_mock_group_rejects_different_period() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    for camera in raw["cameras"][1:]:
        camera["source"].update(
            {"type": "mock", "sync_group": "vehicle_surround", "sync_period_seconds": 191.1}
        )
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_right")
    camera["source"]["sync_period_seconds"] = 190.0

    with pytest.raises(ValueError, match="sync timeline differs"):
        validate_config(raw)


def test_synchronized_mock_group_rejects_different_epoch() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    for camera in raw["cameras"][1:]:
        camera["source"].update(
            {
                "type": "mock",
                "sync_group": "vehicle_surround",
                "sync_period_seconds": 191.1,
                "sync_epoch_seconds": 0.0,
            }
        )
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_left")
    camera["source"]["sync_epoch_seconds"] = 1.0

    with pytest.raises(ValueError, match="sync timeline differs"):
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
    dahua = resolve_camera_config(raw, "DMS")
    assert dahua["fire_smoke"]["tracking"]["confirmation_hits"] == 4
    assert dahua["fire_smoke"]["tracking"]["confirmation_window"] == 6
    assert dahua["fire_smoke"]["dynamics"]["mode"] == "advisory"
    assert dahua["fire_smoke"]["tracking"]["notification_min_duration_seconds"] == 3.0
    assert dahua["functions"]["dms"] is True
    assert dahua["functions"]["smoking_behavior"] is False
    assert dahua["functions"]["fire_smoke"] is False
    dms_models = dahua["dms"]["object_detection"]["models"]
    assert set(dms_models) == {"soham"}
    assert set(dms_models["soham"]["positive_labels"]) == {
        "Distracted",
        "Drinking",
        "Drowsy",
        "Eating",
        "PhoneUse",
        "SafeDriving",
        "Seatbelt",
        "Smoking",
    }
    assert dahua["dms"]["alerts"] == {"on_frames": 3, "off_frames": 2}
    assert dahua["dms"]["event_policy"]["model"] == {
        "min_score": 0.35,
        "require_person_match": True,
        "confirmation_hits": 6,
        "confirmation_window": 10,
        "minimum_duration_seconds": 1.0,
        "candidate_timeout_seconds": 3.0,
        "clear_seconds": 2.0,
        "unknown_timeout_seconds": 1.5,
        "trace_interval_ms": 2000,
    }
    assert dahua["dms"]["event_policy"]["no_seatbelt"]["confirmation_hits"] == 12
    object_models = dahua["smoking_behavior"]["object_detection"]["models"]
    assert set(object_models) == {"soham"}
    assert object_models["soham"]["positive_labels"] == ["Smoking"]
    assert "events" not in dahua


def test_invalid_camera_analysis_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"] = {"functions": {"smoking": {"crop": {"strategy": "upper_body"}}}}

    with pytest.raises(ValueError, match="crop.strategy"):
        resolve_camera_config(raw, "DMS")


def test_invalid_smoking_temporal_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"] = {"functions": {"smoking": {"confirmation": {"hits": 5, "attempts": 4}}}}

    with pytest.raises(ValueError, match="smoking confirmation"):
        resolve_camera_config(raw, "DMS")


def test_invalid_dms_confirmation_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    raw["dms"]["event_policy"]["model"]["confirmation_hits"] = 11
    raw["dms"]["event_policy"]["model"]["confirmation_window"] = 10

    with pytest.raises(ValueError, match="dms.event_policy.model confirmation window"):
        validate_config(raw)


def test_smoking_notification_delay_cannot_precede_confirmation_duration() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"] = {"functions": {"smoking": {
        "temporal": {"minimum_duration_seconds": 1.0},
        "lifecycle": {"notification_min_duration_seconds": 0.5},
    }}}

    with pytest.raises(ValueError, match="notification_min_duration_seconds"):
        resolve_camera_config(raw, "DMS")


def test_all_profiles_keep_dynamics_in_advisory_mode() -> None:
    root = Path(__file__).parents[2] / "config"
    production = resolve_camera_config(
        load_raw_config(root / "production.yaml"), "DMS"
    )
    dev = resolve_camera_config(load_raw_config(root / "dev.yaml"), "DMS")

    assert production["fire_smoke"]["dynamics"]["mode"] == "advisory"
    assert dev["fire_smoke"]["dynamics"]["mode"] == "advisory"


def test_invalid_fire_dynamics_vote_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"] = {"functions": {"fire_smoke": {"dynamics": {
        "confirmation_votes": 6, "confirmation_window": 5,
    }}}}

    with pytest.raises(ValueError, match="confirmation_votes"):
        resolve_camera_config(raw, "DMS")


def test_fire_dynamics_hard_enforcement_is_rejected() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    camera["analysis"] = {"functions": {"fire_smoke": {"dynamics": {"enforce": True}}}}

    with pytest.raises(ValueError, match="hard enforcement is unsupported"):
        resolve_camera_config(raw, "DMS")
