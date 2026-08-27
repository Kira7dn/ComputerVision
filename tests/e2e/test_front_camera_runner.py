from __future__ import annotations

import pytest

from .run_front_camera_jetson import status_root


def test_front_camera_runner_uses_profile_specific_status_root() -> None:
    assert status_root("production") == "/opt/ls-vision"
    assert status_root("development") == "/opt/ls-vision-dev"
    with pytest.raises(ValueError, match="unknown front-camera profile"):
        status_root("dev")
