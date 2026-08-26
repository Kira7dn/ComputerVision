"""Reconcile one isolated DeepStream worker for every configured camera."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from adapters.persistence.evidence_repository import run_directory, write_manifest
from application.pipeline_compiler import compile_camera_plan
from bootstrap.config import (
    camera_ids,
    load_raw_config,
    resolve_camera_config,
    validate_config,
)
from bootstrap.lifecycle import install_shutdown_handlers
from domain.pipeline_plan import CameraExecutionPlan

POLL_SECONDS = 0.5
STABLE_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class CameraWorkerSpec:
    camera_id: str
    plan: CameraExecutionPlan

    @property
    def media_only(self) -> bool:
        return self.plan.media_only


def compile_worker_specs(config: dict[str, Any]) -> dict[str, CameraWorkerSpec]:
    return {
        camera_id: CameraWorkerSpec(
            camera_id,
            compile_camera_plan(resolve_camera_config(config, camera_id)),
        )
        for camera_id in camera_ids(config)
    }


def active_camera_definitions(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Project the accepted config without exposing source credentials."""
    raw_cameras = {
        str(item.get("id")): item
        for item in config.get("cameras", []) or []
        if isinstance(item, dict)
    }
    definitions: list[dict[str, Any]] = []
    for camera_id in camera_ids(config):
        resolved = resolve_camera_config(config, camera_id)
        input_config = resolved.get("input", {}) or {}
        output = resolved.get("output", {}) or {}
        raw_camera = raw_cameras.get(camera_id, {})
        source = input_config.get("mock_video") or input_config.get("rtsp_url")
        if source and not input_config.get("mock_video"):
            parsed = urlsplit(str(source))
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = f"{host}:{parsed.port}" if parsed.port else host
            source = urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
        definitions.append(
            {
                "id": camera_id,
                "display_name": raw_camera.get("display_name"),
                "source": source,
                "source_type": input_config.get("mode", "rtsp"),
                "media_only": bool(input_config.get("media_only", False)),
                "mock_sync_group": input_config.get("mock_sync_group") or None,
                "mock_sync_period_seconds": input_config.get(
                    "mock_sync_period_seconds"
                ),
                "mock_sync_epoch_seconds": input_config.get(
                    "mock_sync_epoch_seconds"
                ),
                "output": output.get("rtsp_url"),
                "dashboard_output": output.get("dashboard_rtsp_url")
                or output.get("rtsp_url"),
                "output_video_published": bool(output.get("publish_video", True)),
                "functions": resolved.get("functions", {}),
            }
        )
    return definitions


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


class CameraSupervisor:
    def __init__(self, config_path: Path, raw_config: dict[str, Any], run_id: str) -> None:
        self.config_path = config_path
        self.raw_config = raw_config
        self.run_id = run_id
        self.run_root = run_directory(raw_config, run_id)
        self.specs = compile_worker_specs(raw_config)
        self.active_cameras = active_camera_definitions(raw_config)
        self.workers: dict[str, subprocess.Popen[bytes]] = {}
        self.restart_at: dict[str, float] = {
            camera_id: 0.0 for camera_id, spec in self.specs.items() if not spec.media_only
        }
        self.restart_attempts: dict[str, int] = dict.fromkeys(self.restart_at, 0)
        self.worker_started_at: dict[str, float] = {}
        self.worker_epochs: dict[str, int] = dict.fromkeys(self.restart_at, 0)
        self.generation = 1
        self.reload_error: str | None = None
        self.last_restarted_cameras: list[str] = []
        runtime = raw_config.get("runtime", {}) or {}
        evidence = raw_config.get("evidence", {}) or {}
        self.active_runtime = {
            "status_directory": runtime.get("status_directory"),
            "evidence_directory": evidence.get("directory"),
            "evidence_prefix": evidence.get("prefix", "snapshots-acceptance"),
        }
        self.status_path = Path(
            str(runtime.get("status_directory", "/opt/ls-vision/data/status"))
        ) / "runner.json"
        self.observed_signature = _file_signature(config_path)
        self.stopping = False

    @property
    def worker_ids(self) -> list[str]:
        return [camera_id for camera_id, spec in self.specs.items() if not spec.media_only]

    def _command_for(self, camera_id: str) -> list[str]:
        self.worker_epochs[camera_id] = self.worker_epochs.get(camera_id, 0) + 1
        return [
            sys.executable,
            "-m",
            "application.camera_runtime",
            "--config",
            str(self.config_path),
            "--camera-id",
            camera_id,
            "--run-id",
            self.run_id,
            "--worker-epoch",
            f"{camera_id}-{self.worker_epochs[camera_id]}-{uuid.uuid4().hex[:6]}",
            "--config-generation",
            str(self.generation),
            "--expected-plan-hash",
            self.specs[camera_id].plan.plan_hash,
        ]

    def start_worker(self, camera_id: str) -> None:
        worker = subprocess.Popen(
            self._command_for(camera_id),
            cwd=str(Path(__file__).resolve().parents[1]),
            env=_environment(),
        )
        self.workers[camera_id] = worker
        self.worker_started_at[camera_id] = time.monotonic()
        print(
            f"[supervisor] started camera={camera_id} pid={worker.pid} "
            f"epoch={self.worker_epochs[camera_id]} generation={self.generation} "
            f"plan={self.specs[camera_id].plan.plan_hash[:12]}",
            flush=True,
        )

    def stop_worker(self, camera_id: str) -> None:
        worker = self.workers.pop(camera_id, None)
        self.worker_started_at.pop(camera_id, None)
        if worker is None or worker.poll() is not None:
            return
        worker.terminate()
        try:
            worker.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=2)

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        for camera_id in list(self.workers):
            self.stop_worker(camera_id)

    def start(self) -> None:
        write_manifest(self.raw_config, self.run_id, self.run_root)
        print(f"run_id={self.run_id} evidence_root={self.run_root}", flush=True)
        for camera_id in self.worker_ids:
            self.start_worker(camera_id)
        self.write_status()

    def _load_candidate(self) -> tuple[dict[str, Any], dict[str, CameraWorkerSpec]]:
        candidate = validate_config(load_raw_config(self.config_path), self.config_path)
        return candidate, compile_worker_specs(candidate)

    def reload_if_changed(self) -> None:
        try:
            signature = _file_signature(self.config_path)
        except OSError as exc:
            self.reload_error = f"config stat failed: {exc}"
            self.write_status()
            return
        if signature == self.observed_signature:
            return
        self.observed_signature = signature
        try:
            candidate, candidate_specs = self._load_candidate()
            timeline_changes = sorted(
                camera_id
                for camera_id in set(self.specs) | set(candidate_specs)
                if (
                    self.specs.get(camera_id).plan.timeline_contract
                    if camera_id in self.specs
                    else None
                )
                != (
                    candidate_specs.get(camera_id).plan.timeline_contract
                    if camera_id in candidate_specs
                    else None
                )
            )
            if timeline_changes:
                raise ValueError(
                    "timeline config change requires service restart: "
                    + ", ".join(timeline_changes)
                )
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            self.reload_error = str(exc)
            print(f"[supervisor] config reload rejected: {exc}", flush=True)
            self.write_status()
            return

        old_worker_ids = set(self.worker_ids)
        new_worker_ids = {
            camera_id for camera_id, spec in candidate_specs.items() if not spec.media_only
        }
        changed = {
            camera_id
            for camera_id in old_worker_ids & new_worker_ids
            if self.specs[camera_id].plan.plan_hash
            != candidate_specs[camera_id].plan.plan_hash
        }
        stopped = sorted((old_worker_ids - new_worker_ids) | changed)
        started = sorted((new_worker_ids - old_worker_ids) | changed)
        for camera_id in stopped:
            self.stop_worker(camera_id)

        self.raw_config = candidate
        self.specs = candidate_specs
        self.active_cameras = active_camera_definitions(candidate)
        runtime = candidate.get("runtime", {}) or {}
        evidence = candidate.get("evidence", {}) or {}
        self.active_runtime = {
            "status_directory": runtime.get("status_directory"),
            "evidence_directory": evidence.get("directory"),
            "evidence_prefix": evidence.get("prefix", "snapshots-acceptance"),
        }
        self.generation += 1
        self.reload_error = None
        self.last_restarted_cameras = sorted(changed | (old_worker_ids ^ new_worker_ids))
        for camera_id in new_worker_ids:
            self.restart_at.setdefault(camera_id, 0.0)
            self.restart_attempts.setdefault(camera_id, 0)
            self.worker_epochs.setdefault(camera_id, 0)
        for camera_id in started:
            self.start_worker(camera_id)
        write_manifest(self.raw_config, self.run_id, self.run_root)
        print(
            f"[supervisor] config generation={self.generation} "
            f"restarted={self.last_restarted_cameras}",
            flush=True,
        )
        self.write_status()

    def maintain_workers(self) -> None:
        now = time.monotonic()
        for camera_id in self.worker_ids:
            worker = self.workers.get(camera_id)
            if worker is not None and worker.poll() is not None:
                return_code = int(worker.returncode or 0)
                uptime = now - self.worker_started_at.get(camera_id, now)
                if uptime >= STABLE_SECONDS:
                    self.restart_attempts[camera_id] = 0
                attempt = self.restart_attempts.get(camera_id, 0)
                delay = min(10.0, 2.0 * (2 ** min(attempt, 2)))
                self.restart_attempts[camera_id] = attempt + 1
                self.restart_at[camera_id] = now + delay
                print(
                    f"[supervisor] camera={camera_id} exited code={return_code}; "
                    f"restart_in={delay:.1f}s",
                    flush=True,
                )
                self.workers.pop(camera_id, None)
                self.worker_started_at.pop(camera_id, None)
            if camera_id not in self.workers and now >= self.restart_at.get(camera_id, 0.0):
                self.start_worker(camera_id)

    def write_status(self) -> None:
        _write_json(
            self.status_path,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "updated_at": time.time(),
                "config_generation": self.generation,
                "reload_error": self.reload_error,
                "last_restarted_cameras": self.last_restarted_cameras,
                "active_cameras": self.active_cameras,
                "active_runtime": self.active_runtime,
                "cameras": {
                    camera_id: {
                        **spec.plan.status(),
                        "config_generation": self.generation,
                        "media_only": spec.media_only,
                        "pid": (
                            self.workers[camera_id].pid
                            if camera_id in self.workers
                            and self.workers[camera_id].poll() is None
                            else None
                        ),
                        "worker_epoch": self.worker_epochs.get(camera_id, 0),
                    }
                    for camera_id, spec in self.specs.items()
                },
            },
        )


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    libraries = [
        "/opt/ls-vision/runtime/deepstream/lib",
        "/opt/ls-vision/runtime/deepstream/lib/gst-plugins",
        "/opt/ls-vision/runtime/triton/lib",
        "/usr/local/cuda/lib64",
        "/usr/lib/aarch64-linux-gnu/nvidia",
        "/usr/lib/aarch64-linux-gnu",
        "/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib",
        "/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib",
        "/usr/local/lib/python3.10/dist-packages/nvidia/cuda_nvrtc/lib",
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
    run_id = args.run_id or (
        f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    )
    supervisor = CameraSupervisor(args.config, raw_config, run_id)
    install_shutdown_handlers(supervisor.stop)
    try:
        supervisor.start()
        while not supervisor.stopping:
            supervisor.reload_if_changed()
            supervisor.maintain_workers()
            supervisor.write_status()
            time.sleep(POLL_SECONDS)
    finally:
        supervisor.stop()
        try:
            supervisor.status_path.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
