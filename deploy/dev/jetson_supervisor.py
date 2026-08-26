"""Supervise the native Jetson development runtime.

Vite owns frontend HMR on the workstation. This standard-library-only
supervisor owns the Jetson MediaMTX, dashboard API and DeepStream runner. Source
changes restart affected services; YAML changes are reconciled per camera by
the runner unless they alter the synchronized timeline contract.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

IGNORED_DIRECTORIES = {".git", ".pytest_cache", "__pycache__", "dist", "node_modules"}
POLL_SECONDS = 0.75
GRACE_SECONDS = 8.0
TIMELINE_STARTUP_SECONDS = 60.0


def file_snapshot(paths: list[Path], root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for path in paths:
        candidates = [path] if path.is_file() else path.rglob("*")
        for candidate in candidates:
            if not candidate.is_file() or any(part in IGNORED_DIRECTORIES for part in candidate.parts):
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            snapshot[candidate.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def wait_for_timeline(path: Path, process: ManagedProcess) -> None:
    deadline = time.monotonic() + TIMELINE_STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.exited():
            raise RuntimeError("mock timeline exited before becoming ready")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            updated_at = float(payload.get("updated_at", 0.0) or 0.0)
            if bool(payload.get("ready")) and 0.0 <= time.time() - updated_at <= 1.0:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise TimeoutError("mock timeline did not become ready before runner startup")


class ManagedProcess:
    def __init__(self, name: str, command: list[str], log_path: Path, cwd: Path, env: dict[str, str]):
        self.name = name
        self.command = command
        self.log_path = log_path
        self.cwd = cwd
        self.env = env
        self.process: subprocess.Popen[bytes] | None = None
        self._log = None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab", buffering=0)
        self._log.write(
            f"\n--- Jetson supervisor start {self.name} {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ---\n".encode()
        )
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self._close_log()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=GRACE_SECONDS)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        finally:
            self._close_log()

    def restart(self) -> None:
        self.stop()
        self.start()

    def exited(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--mediamtx-config", type=Path, required=True)
    parser.add_argument("--mediamtx-bin", type=Path, required=True)
    parser.add_argument("--mock-media-root", type=Path, default=None)
    parser.add_argument("--mock-media-port", type=int, default=18081)
    parser.add_argument("--pid-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # Keep the /current symlink spelling consistent with the systemd config
    # path; resolving only root makes Path.relative_to reject the config path.
    root = args.root.absolute()
    runtime = args.runtime
    runtime_logs = runtime / "logs"
    runtime_status = runtime / "status"
    runtime_logs.mkdir(parents=True, exist_ok=True)
    runtime_status.mkdir(parents=True, exist_ok=True)
    args.pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")

    environment = os.environ.copy()
    environment.pop("NVDS_ENABLE_LATENCY_MEASUREMENT", None)
    environment.pop("NVDS_ENABLE_COMPONENT_LATENCY_MEASUREMENT", None)
    environment.update(
        {
            "PYTHONPATH": str(root / "app" / "src"),
            "CAMERA_CONFIG": str(args.config),
            "CAMERA_WEB_ROOT": str(root / "app" / "web"),
        }
    )
    env_file = root / ".env.local"
    if env_file.is_file():
        environment["CAMERA_ENV_FILE"] = str(env_file)

    processes = {
        "mediamtx": ManagedProcess(
            "mediamtx",
            [str(args.mediamtx_bin), str(args.mediamtx_config)],
            runtime_logs / "mediamtx.log",
            root,
            environment,
        ),
        "dashboard": ManagedProcess(
            "dashboard",
            [sys.executable, "-m", "interfaces.dashboard_api"],
            runtime_logs / "dashboard.log",
            root,
            environment,
        ),
        **(
            {
                "mock_media": ManagedProcess(
                    "mock-media",
                    [
                        sys.executable,
                        "-m",
                        "interfaces.mock_media_server",
                        "--root",
                        str(args.mock_media_root),
                        "--port",
                        str(args.mock_media_port),
                    ],
                    runtime_logs / "mock-media.log",
                    root,
                    environment,
                )
            }
            if args.mock_media_root is not None
            else {}
        ),
        "timeline": ManagedProcess(
            "mock-timeline",
            [
                sys.executable,
                "-m",
                "application.mock_timeline_runtime",
                "--config",
                str(args.config),
                "--preserve-publishers-on-exit",
            ],
            runtime_logs / "mock-timeline.log",
            root,
            environment,
        ),
        "runner": ManagedProcess(
            "runner",
            [sys.executable, "-m", "runner", "--config", str(args.config)],
            runtime_logs / "pipeline.log",
            root,
            environment,
        ),
    }
    stop_requested = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    watched_paths = [
        root / "app" / "src",
        root / "app" / "config",
        args.config,
        env_file,
    ]
    try:
        config_relative = args.config.absolute().relative_to(root).as_posix()
    except ValueError:
        config_relative = ""
    previous_snapshot = file_snapshot(watched_paths, root)
    try:
        for name in ("mediamtx", "dashboard", "mock_media", "timeline"):
            process = processes.get(name)
            if process is not None:
                process.start()
        wait_for_timeline(runtime_status / "mock-timeline.json", processes["timeline"])
        processes["runner"].start()
        while not stop_requested.wait(POLL_SECONDS):
            current_snapshot = file_snapshot(watched_paths, root)
            changed_paths = {
                path
                for path in set(current_snapshot) | set(previous_snapshot)
                if current_snapshot.get(path) != previous_snapshot.get(path)
            }
            previous_snapshot = current_snapshot

            if changed_paths:
                config_changed = any(
                    path.startswith("app/config/")
                    or (config_relative and path == config_relative)
                    for path in changed_paths
                )
                environment_changed = ".env.local" in changed_paths
                source_changed = any(path.startswith("app/src/") for path in changed_paths)
                timeline_changed = any(
                    path == "app/src/application/mock_timeline_runtime.py"
                    or path.startswith("app/src/adapters/media/")
                    or path == "app/src/domain/mock_timeline.py"
                    for path in changed_paths
                )
                mock_media_changed = any(
                    path == "app/src/interfaces/mock_media_server.py"
                    for path in changed_paths
                )
                if source_changed or environment_changed:
                    processes["dashboard"].restart()
                if timeline_changed:
                    processes["timeline"].restart()
                    wait_for_timeline(
                        runtime_status / "mock-timeline.json",
                        processes["timeline"],
                    )
                if source_changed or environment_changed:
                    processes["runner"].restart()
                if (mock_media_changed or environment_changed) and "mock_media" in processes:
                    processes["mock_media"].restart()
                if config_changed:
                    # The runner validates and reconciles config changes per camera.
                    # Timeline-contract changes are rejected there and require an
                    # explicit service restart so all synchronized publishers move
                    # to the new epoch/period together.
                    print("[dev-supervisor] YAML change delegated to camera runner", flush=True)
            for process in processes.values():
                if process.exited() and not stop_requested.is_set():
                    process.start()
    finally:
        for process in reversed(list(processes.values())):
            process.stop()
        try:
            args.pid_file.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
