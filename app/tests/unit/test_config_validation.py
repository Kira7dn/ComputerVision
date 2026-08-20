from pathlib import Path

import pytest

from bootstrap.config import load_raw_config, validate_config


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
