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
    assert "channel=5" in dev["cameras"][0]["source"]["url"]
    assert "channel=5" in production["cameras"][0]["source"]["discovery"]["rtsp_path"]
    for profile in (dev, production):
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
    dev_front = next(
        camera for camera in dev["cameras"] if camera["id"] == "camera_front"
    )
    production_front = next(
        camera for camera in production["cameras"] if camera["id"] == "camera_front"
    )
    assert (
        dev_front["front_assistance"]["overlay"]
        == production_front["front_assistance"]["overlay"]
    )
    assert (
        dev_front["front_assistance"]["alerts"]
        == production_front["front_assistance"]["alerts"]
    )


def test_only_dms_is_mirrored_before_the_vision_pipeline() -> None:
    root = Path(__file__).parents[2] / "config"

    for profile_name in ("dev.yaml", "production.yaml"):
        raw = load_raw_config(root / profile_name)
        resolved = {
            camera_id: resolve_camera_config(raw, camera_id)
            for camera_id in ["DMS", "camera_front", "camera_back", "camera_left", "camera_right"]
        }

        assert resolved["DMS"]["input"]["mirror_horizontal"] is True
        assert all(
            resolved[camera_id]["input"]["mirror_horizontal"] is False
            for camera_id in ("camera_front", "camera_back", "camera_left", "camera_right")
        )


def test_source_mirror_horizontal_must_be_boolean() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")
    dms = next(camera for camera in raw["cameras"] if camera["id"] == "DMS")
    dms["source"]["mirror_horizontal"] = "true"

    with pytest.raises(ValueError, match="source.mirror_horizontal must be boolean"):
        resolve_camera_config(raw, "DMS")


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


def test_loopback_input_rtsp_base_can_be_isolated_without_changing_external_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.setenv("CAMERA_INPUT_RTSP_BASE", "rtsp://127.0.0.1:28554")
    raw = load_raw_config(root / "config" / "dev.yaml")

    front = resolve_camera_config(raw, "camera_front")
    dms = resolve_camera_config(raw, "DMS")

    assert front["input"]["rtsp_url"] == "rtsp://127.0.0.1:28554/camera_front_raw"
    assert "channel=5" in dms["input"]["rtsp_url"]


def test_metadata_base_can_be_isolated_per_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.setenv("CAMERA_METADATA_ZMQ_BASE", "tcp://127.0.0.1:6555")
    raw = load_raw_config(root / "config" / "dev.yaml")

    dms = resolve_camera_config(raw, "DMS")
    front = resolve_camera_config(raw, "camera_front")

    assert dms["metadata"]["zmq_pub_url"] == "tcp://127.0.0.1:6555"
    assert front["metadata"]["zmq_pub_url"] == "tcp://127.0.0.1:6556"


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
        "max_side": 512,
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
        "lane_min_probability": 0.0,
        "path_half_width_m": 0.9,
        "lead_min_probability": 0.5,
        "road_edge_max_std_m": 0.6,
    }
    alerts = resolved["front_assistance"]["alerts"]
    assert alerts["ldw_confirmation_window"] == 1
    assert alerts["ldw_clear_observations"] == 20
    assert alerts["fcw_brake_probability"] == 0.09
    assert alerts["fcw_clear_observations"] == 20
    assert alerts["lead_ttc_seconds"] == 10.0
    assert alerts["lead_probability"] == 0.65
    assert alerts["lead_clear_observations"] == 20
    assert alerts["edge_max_std_m"] == 1.5
    assert alerts["ldw_lane_probability"] == 0.35
    assert alerts["edge_clear_observations"] == 20
    assert alerts["geometry_baseline_frames"] == 200
    assert alerts["geometry_translation_m"] == 0.25
    assert alerts["geometry_trigger_hits"] == 40
    assert alerts["geometry_trigger_window"] == 50
    assert alerts["geometry_clear_observations"] == 100
    assert resolved["front_assistance"]["max_gap_seconds"] == 0.5
    assert resolved["front_assistance"]["calibration"]["source_width"] == 960
    assert resolved["input"]["mock_video"].endswith("/CAM_FRONT_20FPS_ALL_I.mp4")
    assert resolved["input"]["mock_publisher"] == "packet_copy"
    assert resolved["input"]["fps"] == 20
    assert resolved["input"]["rtsp_url"].endswith("/camera_front_raw")
    assert resolved["output"]["rtsp_url"].endswith("/camera_front")
    assert resolved["output"]["dashboard_rtsp_url"].endswith("/camera_front")
    assert resolved["output"]["publish_video"] is True


@pytest.mark.parametrize(
    ("section", "key", "value"),
    (
        ("overlay", "lead_min_probability", 1.1),
        ("overlay", "road_edge_max_std_m", 0.0),
        ("overlay", "road_edge_max_std_m", float("nan")),
        ("alerts", "ldw_confirmation_hits", 2),
        ("alerts", "fcw_clear_probability", 0.1),
        ("alerts", "lead_ttc_seconds", 30.0),
        ("alerts", "edge_trigger_clearance_m", 4.0),
        ("alerts", "geometry_trigger_hits", 51),
    ),
)
def test_front_thresholds_fail_closed(section: str, key: str, value: float) -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    front = next(camera for camera in raw["cameras"] if camera["id"] == "camera_front")
    front["front_assistance"][section][key] = value

    with pytest.raises(ValueError, match="front_assistance"):
        validate_config(raw)


def test_front_non_finite_calibration_fails_closed() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    front = next(camera for camera in raw["cameras"] if camera["id"] == "camera_front")
    front["front_assistance"]["calibration"]["intrinsics"][0][0] = float("nan")

    with pytest.raises(ValueError, match="calibration"):
        validate_config(raw)


def test_dms_output_is_streamable_without_changing_input_resolution() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    resolved = resolve_camera_config(raw, "DMS")

    assert (resolved["input"]["width"], resolved["input"]["height"]) == (1920, 1080)
    assert (resolved["output"]["width"], resolved["output"]["height"]) == (960, 540)
    assert resolved["output"]["bitrate_bps"] == 3_000_000
    assert resolved["output"]["rate_hz"] == 10
    assert resolved["dms"]["face_mesh"]["driver_roi"]["max_side"] == 512
    assert resolved["dms"]["face_mesh"]["interval_ms"] == 150


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
    assert dahua["dms"]["event_policy"]["model"] == {
        "min_score": 0.35,
        "require_person_match": True,
        "confirmation_hits": 2,
        "confirmation_window": 3,
        "minimum_duration_seconds": 0.2,
        "candidate_timeout_seconds": 1.0,
        "clear_seconds": 0.4,
        "unknown_timeout_seconds": 0.8,
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
