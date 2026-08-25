import subprocess

import pytest

from ls_vision.adapters.media import mock_input


def test_wait_for_rtsp_video_requires_a_decodable_video_stream(monkeypatch) -> None:
    attempts = iter(
        [
            subprocess.CompletedProcess([], 1, "", "not ready"),
            subprocess.CompletedProcess([], 0, "video\n", ""),
        ]
    )
    monkeypatch.setattr(mock_input.subprocess, "run", lambda *args, **kwargs: next(attempts))
    monkeypatch.setattr(mock_input.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(mock_input.time, "sleep", lambda _seconds: None)

    mock_input.wait_for_rtsp_video("rtsp://127.0.0.1:8554/mock", timeout_seconds=1)


def test_wait_for_rtsp_video_fails_immediately_if_publisher_exits() -> None:
    with pytest.raises(RuntimeError, match="publisher exited"):
        mock_input.wait_for_rtsp_video(
            "rtsp://127.0.0.1:8554/mock",
            timeout_seconds=1,
            publisher_alive=lambda: False,
        )


def test_wait_for_rtsp_video_uses_gstreamer_without_ffprobe(monkeypatch) -> None:
    captured = {}

    def which(name: str):
        return "/usr/bin/gst-launch-1.0" if name == "gst-launch-1.0" else None

    def run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mock_input.shutil, "which", which)
    monkeypatch.setattr(mock_input.subprocess, "run", run)

    mock_input.wait_for_rtsp_video("rtsp://127.0.0.1:8554/mock", timeout_seconds=1)

    assert captured["command"][0] == "gst-launch-1.0"
    assert "rtph264depay" in captured["command"]
