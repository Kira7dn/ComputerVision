"""Native LS-Vision service owner for dashboard and camera processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from bootstrap.lifecycle import install_shutdown_handlers
from bootstrap.paths import RuntimePaths

STOP_TIMEOUT_SECONDS = 10.0


def main() -> int:
    if os.environ.get("CAMERA_RUNTIME_ROOT"):
        RuntimePaths.from_environment().ensure_writable()
    config = os.environ.get(
        "CAMERA_CONFIG", "/opt/ls-vision/current/app/config/production.yaml"
    )
    processes = [
        subprocess.Popen([sys.executable, "-m", "interfaces.dashboard_api"]),
        subprocess.Popen([sys.executable, "-m", "runner", "--config", config]),
    ]
    mock_media_root = os.environ.get("CAMERA_MOCK_MEDIA_ROOT", "").strip()
    if mock_media_root:
        processes.append(
            subprocess.Popen(
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
        )

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
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    stop(signal.SIGTERM, None)
                    return int(return_code) if return_code != 0 else 1
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
