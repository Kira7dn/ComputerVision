import subprocess

import pytest

from deepstream_safety import mock_input


def test_wait_for_rtsp_video_requires_a_decodable_video_stream(monkeypatch) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess([], 1, "", "not ready"),
            subprocess.CompletedProcess([], 0, "video\n", ""),
        ]
    )
    monkeypatch.setattr(mock_input.subprocess, "run", lambda *args, **kwargs: next(attempts))
    monkeypatch.setattr(mock_input.time, "sleep", lambda _seconds: None)

    mock_input.wait_for_rtsp_video("rtsp://127.0.0.1:8554/mock", timeout_seconds=1)


def test_wait_for_rtsp_video_fails_immediately_if_publisher_exits() -> None:
    with pytest.raises(RuntimeError, match="publisher exited"):
        mock_input.wait_for_rtsp_video(
            "rtsp://127.0.0.1:8554/mock",
            timeout_seconds=1,
            publisher_alive=lambda: False,
        )
