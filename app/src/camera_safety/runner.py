"""Run one isolated DeepStream worker for every configured camera."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from camera_safety.adapters.persistence.evidence_repository import run_directory, write_manifest
from camera_safety.bootstrap.config import camera_ids, load_raw_config, validate_config


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    libraries = [
        "/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib",
        "/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib",
        "/usr/local/lib/python3.10/dist-packages/nvidia/cuda_nvrtc/lib",
        "/usr/lib/wsl/lib",
        "/usr/local/cuda/lib64",
        "/usr/local/lib/python3.10/dist-packages/tensorrt_libs",
    ]
    existing = env.get("LD_LIBRARY_PATH")
    env["LD_LIBRARY_PATH"] = ":".join(libraries + ([existing] if existing else []))
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args()

    raw_config = validate_config(load_raw_config(args.config), args.config)
    ids = camera_ids(raw_config)
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    run_root = run_directory(raw_config, run_id)
    write_manifest(raw_config, run_id, run_root)
    print(f"run_id={run_id} evidence_root={run_root}", flush=True)
    workers: dict[str, subprocess.Popen[bytes]] = {}
    restart_at: dict[str, float] = {camera_id: 0.0 for camera_id in ids}
    restart_attempts: dict[str, int] = {camera_id: 0 for camera_id in ids}
    worker_started_at: dict[str, float] = {}
    worker_epochs: dict[str, int] = {camera_id: 0 for camera_id in ids}
    stopping = False

    def command_for(camera_id: str) -> list[str]:
        worker_epochs[camera_id] += 1
        return [
            sys.executable,
            "-m",
            "camera_safety.application.camera_worker",
            "--config",
            str(args.config),
            "--camera-id",
            camera_id,
            "--run-id",
            run_id,
            "--worker-epoch",
            f"{camera_id}-{worker_epochs[camera_id]}-{uuid.uuid4().hex[:6]}",
        ]

    def start_worker(camera_id: str) -> None:
        worker = subprocess.Popen(
            command_for(camera_id),
            cwd=str(Path(__file__).resolve().parents[2]),
            env=_environment(),
        )
        workers[camera_id] = worker
        worker_started_at[camera_id] = time.monotonic()
        print(
            f"[supervisor] started camera={camera_id} pid={worker.pid} "
            f"epoch={worker_epochs[camera_id]}",
            flush=True,
        )

    def stop_workers(*_: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for worker in workers.values():
            if worker.poll() is None:
                worker.terminate()

    signal.signal(signal.SIGINT, stop_workers)
    signal.signal(signal.SIGTERM, stop_workers)
    try:
        for camera_id in ids:
            start_worker(camera_id)
        while not stopping:
            now = time.monotonic()
            for camera_id in ids:
                worker = workers.get(camera_id)
                if worker is not None and worker.poll() is not None:
                    return_code = int(worker.returncode or 0)
                    uptime = now - worker_started_at.get(camera_id, now)
                    if uptime >= 30.0:
                        restart_attempts[camera_id] = 0
                    attempt = restart_attempts[camera_id]
                    delay = min(10.0, 2.0 * (2**min(attempt, 2)))
                    restart_attempts[camera_id] = attempt + 1
                    restart_at[camera_id] = now + delay
                    print(
                        f"[supervisor] camera={camera_id} exited code={return_code}; "
                        f"restart_in={delay:.1f}s",
                        flush=True,
                    )
                    workers.pop(camera_id, None)
                    worker_started_at.pop(camera_id, None)
                if camera_id not in workers and now >= restart_at[camera_id]:
                    start_worker(camera_id)
            time.sleep(0.5)
    finally:
        stop_workers()
        for worker in workers.values():
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
