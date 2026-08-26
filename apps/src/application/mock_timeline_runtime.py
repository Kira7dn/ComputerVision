"""Own synchronized mock publishers independently from vision workers."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters.media.mock_input import wait_for_rtsp_video
from bootstrap.config import camera_ids, load_raw_config, resolve_camera_config
from bootstrap.logging import configure_logging
from bootstrap.paths import RuntimePaths

STATUS_FRESH_SECONDS = 1.0
POLL_SECONDS = 0.25
STABLE_SECONDS = 30.0


@dataclass(frozen=True)
class TimelineCamera:
    camera_id: str
    group: str
    period_seconds: float
    epoch_seconds: float
    media_only: bool
    mock_video: Path
    rtsp_url: str
    fps: int
    publisher_mode: str = "frame_encode"


def discover_timeline_cameras(config: dict[str, Any]) -> list[TimelineCamera]:
    cameras: list[TimelineCamera] = []
    group_contracts: dict[str, tuple[float, float]] = {}
    for camera_id in camera_ids(config):
        resolved = resolve_camera_config(config, camera_id)
        source = resolved.get("input", {}) or {}
        if str(source.get("mode", "rtsp")).lower() != "mock":
            continue
        group = str(source.get("mock_sync_group", "")).strip()
        period = float(source.get("mock_sync_period_seconds", 0.0))
        epoch = float(source.get("mock_sync_epoch_seconds", 0.0))
        if not group or period <= 0.0:
            continue
        contract = (period, epoch)
        previous = group_contracts.setdefault(group, contract)
        if previous != contract:
            raise ValueError(f"camera {camera_id} timeline differs from group {group}")
        mock_video_value = str(source.get("mock_video", "")).strip()
        if not mock_video_value:
            raise ValueError(f"camera {camera_id} synchronized mock requires mock_video")
        mock_video = Path(mock_video_value)
        publisher_mode = str(source.get("mock_publisher", "frame_encode")).strip().lower()
        if publisher_mode not in {"frame_encode", "packet_copy"}:
            raise ValueError(
                f"camera {camera_id} mock_publisher must be frame_encode or packet_copy"
            )
        fps = int(source.get("fps", 0) or 0)
        if fps <= 0:
            raise ValueError(f"camera {camera_id} synchronized mock requires positive fps")
        cameras.append(
            TimelineCamera(
                camera_id=camera_id,
                group=group,
                period_seconds=period,
                epoch_seconds=epoch,
                media_only=bool(source.get("media_only", False)),
                mock_video=mock_video,
                rtsp_url=str(source.get("rtsp_url", "")),
                fps=fps,
                publisher_mode=publisher_mode,
            )
        )
    return cameras


def _read_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


class ManagedPublisher:
    def __init__(self, camera: TimelineCamera, status_path: Path) -> None:
        self.camera = camera
        self.status_path = status_path
        self.process: subprocess.Popen[bytes] | None = None
        self.adopted_pid: int | None = None
        self.restart_attempts = 0
        self.restart_at = 0.0
        self.started_at = 0.0
        self.stream_verified = False
        self.next_probe_at = 0.0

    def start(self) -> None:
        existing = _read_status(self.status_path)
        existing_pid = int(existing.get("pid", 0) or 0)
        updated_at = float(existing.get("updated_at", 0.0) or 0.0)
        if (
            existing_pid > 0
            and existing.get("camera_id") == self.camera.camera_id
            and existing.get("sync_group") == self.camera.group
            and existing.get("publisher_mode", "frame_encode")
            == self.camera.publisher_mode
            and bool(existing.get("ready"))
            and 0.0 <= time.time() - updated_at <= STATUS_FRESH_SECONDS
            and self._pid_alive(existing_pid)
        ):
            self.process = None
            self.adopted_pid = existing_pid
            self.started_at = time.monotonic()
            self.stream_verified = False
            self.next_probe_at = 0.0
            return
        self.status_path.unlink(missing_ok=True)
        module = (
            "adapters.media.gstreamer_packet_publisher"
            if self.camera.publisher_mode == "packet_copy"
            else "adapters.media.gstreamer_mock_publisher"
        )
        command = [
            sys.executable,
            "-m",
            module,
            "--input",
            str(self.camera.mock_video),
            "--output",
            self.camera.rtsp_url,
            "--fps",
            str(self.camera.fps),
            "--loop",
            "--sync-period",
            str(self.camera.period_seconds),
            "--sync-epoch",
            str(self.camera.epoch_seconds),
            "--camera-id",
            self.camera.camera_id,
            "--sync-group",
            self.camera.group,
            "--status-path",
            str(self.status_path),
        ]
        self.process = subprocess.Popen(command)
        self.adopted_pid = None
        self.started_at = time.monotonic()
        self.stream_verified = False
        self.next_probe_at = 0.0

    def stop(self) -> None:
        process = self.process
        if process is None:
            if self.adopted_pid is not None and self._pid_alive(self.adopted_pid):
                os.kill(self.adopted_pid, signal.SIGTERM)
            return
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        return self.adopted_pid is not None and self._pid_alive(self.adopted_pid)

    def maintain(self, now: float) -> None:
        if self.process is None and self.adopted_pid is None:
            if now >= self.restart_at:
                self.start()
            return
        if not self.alive():
            uptime = now - self.started_at
            if uptime >= STABLE_SECONDS:
                self.restart_attempts = 0
            delay = min(10.0, float(2 ** min(self.restart_attempts, 3)))
            self.restart_attempts += 1
            self.restart_at = now + delay
            self.process = None
            self.adopted_pid = None
            self.stream_verified = False
            return
        status = _read_status(self.status_path)
        if (
            not self.stream_verified
            and bool(status.get("ready"))
            and now >= self.next_probe_at
        ):
            try:
                wait_for_rtsp_video(
                    self.camera.rtsp_url,
                    timeout_seconds=5.0,
                    probe_timeout_seconds=1.0,
                    retry_delay_seconds=0.25,
                    publisher_alive=lambda: (
                        self.alive()
                    ),
                )
                self.stream_verified = True
            except (OSError, RuntimeError, TimeoutError):
                self.next_probe_at = now + 2.0

    def status(self, now_epoch: float) -> dict[str, Any]:
        payload = _read_status(self.status_path)
        updated_at = float(payload.get("updated_at", 0.0) or 0.0)
        process_alive = self.alive()
        fresh = 0.0 <= now_epoch - updated_at <= STATUS_FRESH_SECONDS
        return {
            "mode": "publisher",
            "ready": bool(
                process_alive
                and self.stream_verified
                and fresh
                and payload.get("ready")
            ),
            "pid": (
                self.process.pid
                if process_alive and self.process is not None
                else self.adopted_pid if process_alive else None
            ),
            "updated_at": updated_at or None,
            "normalized_phase": payload.get("normalized_phase"),
            "current_frame_index": payload.get("current_frame_index"),
            "frame_count": payload.get("frame_count"),
            "output_frame_count": payload.get("output_frame_count"),
            "frame_timing_samples": payload.get("frame_timing_samples", []),
            "publisher_mode": payload.get("publisher_mode", self.camera.publisher_mode),
            "rtsp_url": self.camera.rtsp_url,
        }


def _aggregate_status(
    cameras: list[TimelineCamera],
    publishers: dict[str, ManagedPublisher],
) -> dict[str, Any]:
    now = time.time()
    groups: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        group = groups.setdefault(
            camera.group,
            {
                "locked": True,
                "period_seconds": camera.period_seconds,
                "epoch_seconds": camera.epoch_seconds,
                "normalized_phase": (
                    ((now - camera.epoch_seconds) % camera.period_seconds)
                    / camera.period_seconds
                ),
                "cameras": {},
            },
        )
        camera_status = publishers[camera.camera_id].status(now)
        group["cameras"][camera.camera_id] = camera_status
        group["locked"] = bool(group["locked"] and camera_status["ready"])
    ready = all(bool(group["locked"]) for group in groups.values())
    return {
        "schema_version": 1,
        "ready": ready,
        "pid": os.getpid(),
        "updated_at": now,
        "groups": groups,
    }


def run(
    config_path: Path,
    status_path: Path | None = None,
    *,
    preserve_publishers_on_exit: bool = False,
) -> int:
    raw_config = load_raw_config(config_path)
    cameras = discover_timeline_cameras(raw_config)
    resolved_status_path = status_path or (
        RuntimePaths.from_environment().status / "mock-timeline.json"
    )
    publisher_status_dir = resolved_status_path.parent / "mock-timeline-publishers"
    publishers = {
        camera.camera_id: ManagedPublisher(
            camera,
            publisher_status_dir / f"{camera.camera_id}.json",
        )
        for camera in cameras
    }
    resolved_status_path.unlink(missing_ok=True)
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        for publisher in publishers.values():
            publisher.start()
        while not stopping:
            now = time.monotonic()
            for publisher in publishers.values():
                publisher.maintain(now)
            _write_status(resolved_status_path, _aggregate_status(cameras, publishers))
            time.sleep(POLL_SECONDS)
    finally:
        if not preserve_publishers_on_exit:
            for publisher in publishers.values():
                publisher.stop()
        final_status = _aggregate_status(cameras, publishers)
        final_status["ready"] = False
        for group in final_status["groups"].values():
            group["locked"] = False
        _write_status(resolved_status_path, final_status)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--preserve-publishers-on-exit", action="store_true")
    args = parser.parse_args()
    configure_logging()
    return run(
        args.config,
        args.status_path,
        preserve_publishers_on_exit=args.preserve_publishers_on_exit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
