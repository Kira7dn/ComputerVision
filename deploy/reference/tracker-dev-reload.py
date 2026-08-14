"""Restart the development tracker container when mounted Python changes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from watchfiles import PythonFilter, watch


def tracker_command() -> list[str]:
    """Build the tracker command from container environment."""
    node_id = os.environ.get("TRACKER_NODE_ID")
    if not node_id:
        raise RuntimeError("TRACKER_NODE_ID is required")
    return [
        "python3",
        "-u",
        "-m",
        "extension.tracker.app",
        "--config",
        "/config/config.yml",
        "--node-id",
        node_id,
        "--bind",
        "0.0.0.0:50052",
        "--spool-dir",
        "/var/lib/camera-tracker/spool",
        "--media-dir",
        "/media/frigate",
        "--certificate",
        "/run/tracker-tls/server.crt",
        "--key",
        "/run/tracker-tls/server.key",
        "--client-ca",
        "/run/tracker-tls/ca.crt",
        "--allow-client",
        "frigate-main",
    ]


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the tracker parent; Docker removes remaining container children."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    """Run tracker until it exits or mounted Python source changes."""
    process = subprocess.Popen(tracker_command())
    stopping = False

    def handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        stop_process(process)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    roots = [Path("/opt/frigate/frigate"), Path("/opt/frigate/extension")]
    for changes in watch(
        *roots,
        watch_filter=PythonFilter(),
        yield_on_timeout=True,
        rust_timeout=1000,
    ):
        if stopping:
            return 0
        return_code = process.poll()
        if return_code is not None:
            return return_code
        if changes:
            print(f"tracker dev reload: {len(changes)} Python change(s)", flush=True)
            stop_process(process)
            # A non-zero PID 1 exit lets Docker restart the whole container,
            # guaranteeing all multiprocessing children are removed.
            return 75
    return 0


if __name__ == "__main__":
    sys.exit(main())
