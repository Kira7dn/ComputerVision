"""Readiness checks for local mock-video RTSP publishers."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable


def wait_for_rtsp_video(
    rtsp_url: str,
    *,
    timeout_seconds: float = 60.0,
    probe_timeout_seconds: float = 3.0,
    retry_delay_seconds: float = 0.5,
    publisher_alive: Callable[[], bool] | None = None,
) -> None:
    """Wait until ffprobe can read a video stream from an RTSP URL."""
    deadline = time.monotonic() + timeout_seconds
    last_error = "RTSP video stream is not ready"
    while time.monotonic() < deadline:
        if publisher_alive is not None and not publisher_alive():
            raise RuntimeError("mock publisher exited before RTSP became ready")
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-rw_timeout",
                    str(int(probe_timeout_seconds * 1_000_000)),
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    rtsp_url,
                ],
                capture_output=True,
                text=True,
                timeout=probe_timeout_seconds + 1.0,
                check=False,
            )
            if result.returncode == 0 and "video" in result.stdout.splitlines():
                return
            last_error = result.stderr.strip() or f"ffprobe exited {result.returncode}"
        except subprocess.TimeoutExpired:
            last_error = "ffprobe timed out"
        time.sleep(retry_delay_seconds)
    raise TimeoutError(f"mock RTSP readiness timed out: {last_error}")
