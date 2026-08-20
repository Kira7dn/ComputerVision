"""Container process owner for dashboard plus the camera supervisor."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def main() -> int:
    config = os.environ.get("CAMERA_CONFIG", "/opt/camera-safety/config/production.yaml")
    processes = [
        subprocess.Popen([sys.executable, "-m", "camera_safety.interfaces.dashboard_api"]),
        subprocess.Popen([sys.executable, "-m", "camera_safety.runner", "--config", config]),
    ]

    def stop(_signum: int, _frame: object) -> None:
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return max(process.wait() for process in processes)
    finally:
        stop(signal.SIGTERM, None)


if __name__ == "__main__":
    raise SystemExit(main())
