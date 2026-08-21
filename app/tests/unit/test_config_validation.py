from pathlib import Path

import pytest

from bootstrap.config import load_raw_config, resolve_camera_config, validate_config


def test_profiles_merge_and_production_has_no_mock_source() -> None:
    dev = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    production = load_raw_config(Path(__file__).parents[2] / "config" / "production.yaml")

    assert dev["profile"] == "dev"
    assert all(camera["source"]["type"] == "mock" for camera in dev["cameras"][:2])
    assert all(camera["source"]["type"] == "rtsp" for camera in production["cameras"])


def test_production_mock_source_is_rejected() -> None:
    config = {
        "profile": "production",
        "cameras": [{"id": "camera", "source": {"type": "mock", "url": "rtsp://x/y"}, "output": {"rtsp_url": "rtsp://x/z"}}],
    }
    with pytest.raises(ValueError, match="cannot use mock"):
        validate_config(config)


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
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")

    safety = resolve_camera_config(raw, "camera_safety")
    dahua = resolve_camera_config(raw, "camera_dahua")

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
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_safety")
    camera["analysis"]["functions"]["smoking"]["crop"]["strategy"] = "upper_body"

    with pytest.raises(ValueError, match="crop.strategy"):
        resolve_camera_config(raw, "camera_safety")


def test_invalid_smoking_temporal_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_safety")
    camera["analysis"]["functions"]["smoking"]["confirmation"] = {
        "hits": 5,
        "attempts": 4,
    }

    with pytest.raises(ValueError, match="smoking confirmation"):
        resolve_camera_config(raw, "camera_safety")


def test_smoking_notification_delay_cannot_precede_confirmation_duration() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
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
        load_raw_config(root / "production.yaml"), "camera_dahua"
    )
    e2e = resolve_camera_config(load_raw_config(root / "e2e.yaml"), "camera_dahua")

    assert production["fire_smoke"]["dynamics"]["mode"] == "advisory"
    assert e2e["fire_smoke"]["dynamics"]["mode"] == "advisory"


def test_invalid_fire_dynamics_vote_window_fails_before_startup() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_dahua")
    camera["analysis"]["functions"]["fire_smoke"]["dynamics"] = {
        "confirmation_votes": 6,
        "confirmation_window": 5,
    }

    with pytest.raises(ValueError, match="confirmation_votes"):
        resolve_camera_config(raw, "camera_dahua")


def test_fire_dynamics_hard_enforcement_is_rejected() -> None:
    raw = load_raw_config(Path(__file__).parents[2] / "config" / "dev.yaml")
    camera = next(item for item in raw["cameras"] if item["id"] == "camera_dahua")
    camera["analysis"]["functions"]["fire_smoke"]["dynamics"] = {
        "enforce": True
    }

    with pytest.raises(ValueError, match="hard enforcement is unsupported"):
        resolve_camera_config(raw, "camera_dahua")
