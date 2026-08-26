"""Native LS-Vision service owner for dashboard and camera processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from bootstrap.lifecycle import install_shutdown_handlers
from bootstrap.paths import RuntimePaths

STOP_TIMEOUT_SECONDS = 10.0
TIMELINE_RESTART_DELAY_SECONDS = 2.0


def _timeline_ready(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    updated_at = float(payload.get("updated_at", 0.0) or 0.0)
    return bool(payload.get("ready")) and 0.0 <= time.time() - updated_at <= 1.0


def _wait_for_timeline(path: Path, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("mock timeline exited before becoming ready")
        if _timeline_ready(path):
            return
        time.sleep(0.25)
    raise TimeoutError("mock timeline did not become ready before runner startup")


def main() -> int:
    if os.environ.get("CAMERA_RUNTIME_ROOT"):
        RuntimePaths.from_environment().ensure_writable()
    config = os.environ.get(
        "CAMERA_CONFIG", "/opt/ls-vision/current/app/config/production.yaml"
    )
    processes: list[subprocess.Popen[bytes]] = []
    dashboard = subprocess.Popen([sys.executable, "-m", "interfaces.dashboard_api"])
    processes.append(dashboard)
    mock_media_process: subprocess.Popen[bytes] | None = None
    mock_media_root = os.environ.get("CAMERA_MOCK_MEDIA_ROOT", "").strip()
    if mock_media_root:
        mock_media_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "interfaces.mock_media_server",
                "--root",
                mock_media_root,
                "--port",
                os.environ.get("CAMERA_MOCK_MEDIA_PORT", "18081"),
            ]
        )
        processes.append(mock_media_process)
    timeline_enabled = os.environ.get("CAMERA_MOCK_TIMELINE_ENABLED", "1") != "0"
    timeline: subprocess.Popen[bytes] | None = None
    timeline_status = RuntimePaths.from_environment().status / "mock-timeline.json"
    if timeline_enabled:
        timeline = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "application.mock_timeline_runtime",
                "--config",
                config,
                "--preserve-publishers-on-exit",
            ]
        )
        processes.append(timeline)
        try:
            _wait_for_timeline(
                timeline_status,
                timeline,
                float(os.environ.get("CAMERA_MOCK_TIMELINE_STARTUP_TIMEOUT", "60")),
            )
        except (RuntimeError, TimeoutError):
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                if process.poll() is None:
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
            raise
    runner = subprocess.Popen([sys.executable, "-m", "runner", "--config", config])
    processes.append(runner)
    critical_processes = [dashboard, runner]
    if mock_media_process is not None:
        critical_processes.append(mock_media_process)
    timeline_restart_at = 0.0

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    install_shutdown_handlers(lambda: stop(signal.SIGTERM, None))
    try:
        while not stopping:
            for process in critical_processes:
                return_code = process.poll()
                if return_code is not None:
                    stop(signal.SIGTERM, None)
                    return int(return_code) if return_code != 0 else 1
            if timeline is not None and timeline.poll() is not None:
                now = time.monotonic()
                if now >= timeline_restart_at:
                    processes.remove(timeline)
                    timeline = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "application.mock_timeline_runtime",
                            "--config",
                            config,
                            "--preserve-publishers-on-exit",
                        ]
                    )
                    processes.append(timeline)
                    timeline_restart_at = now + TIMELINE_RESTART_DELAY_SECONDS
            time.sleep(0.25)
        return 0
    finally:
        stop(signal.SIGTERM, None)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
        for process in processes:
            if process.poll() is None:
                process.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
