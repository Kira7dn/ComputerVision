"""Readiness checks for local mock-video RTSP publishers."""

from __future__ import annotations

import shutil
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
    """Wait until an available native probe can read RTSP video."""
    deadline = time.monotonic() + timeout_seconds
    last_error = "RTSP video stream is not ready"
    while time.monotonic() < deadline:
        if publisher_alive is not None and not publisher_alive():
            raise RuntimeError("mock publisher exited before RTSP became ready")
        try:
            if shutil.which("ffprobe"):
                command = [
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
                ]
                expected_output = "video"
            elif shutil.which("gst-launch-1.0"):
                command = [
                    "gst-launch-1.0",
                    "-q",
                    "rtspsrc",
                    f"location={rtsp_url}",
                    "protocols=tcp",
                    "latency=100",
                    "!",
                    "rtph264depay",
                    "!",
                    "fakesink",
                    "num-buffers=1",
                    "sync=false",
                ]
                expected_output = None
            else:
                raise RuntimeError("neither ffprobe nor gst-launch-1.0 is available")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=probe_timeout_seconds + 1.0,
                check=False,
            )
            if result.returncode == 0 and (
                expected_output is None or expected_output in result.stdout.splitlines()
            ):
                return
            last_error = result.stderr.strip() or f"RTSP probe exited {result.returncode}"
        except subprocess.TimeoutExpired:
            last_error = "RTSP probe timed out"
        time.sleep(retry_delay_seconds)
    raise TimeoutError(f"mock RTSP readiness timed out: {last_error}")
