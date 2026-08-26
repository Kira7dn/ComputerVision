from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, urlparse

import yaml

from bootstrap.config import camera_ids, load_raw_config, resolve_camera_config

APP_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = APP_ROOT.parent
WEB_ROOT = Path(os.environ.get("CAMERA_WEB_ROOT", APP_ROOT / "web"))
# Production deploys the Vite bundle below the release app/web/dist. Keep the source-root
# fallback for native development, where Vite owns frontend serving.
ROOT = WEB_ROOT / "dist" if (WEB_ROOT / "dist" / "dashboard.html").is_file() else WEB_ROOT
CONFIG_PATH = Path(os.environ.get("CAMERA_CONFIG", WORKSPACE_ROOT / "config" / "dev.yaml"))
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
CPU_LOCK = Lock()
GPU_LOCK = Lock()
METRICS_LOCK = Lock()
PREVIOUS_CPU: tuple[int, int] | None = None
PREVIOUS_PROCESSES: dict[int, tuple[int, float]] = {}
GPU_CACHE: tuple[float, dict[str, object]] | None = None
METRICS_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
STREAM_PROBE_LOCK = Lock()
STREAM_PROBE_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
CONFIG_CACHE_LOCK = Lock()
CONFIG_CACHE: tuple[tuple[object, ...], dict[str, object]] | None = None
JOURNAL_CACHE_LOCK = Lock()
JOURNAL_CACHE: dict[Path, dict[str, object]] = {}
EVIDENCE_RUN_CACHE_LOCK = Lock()
EVIDENCE_RUN_CACHE: dict[tuple[str, str], tuple[int, Path | None]] = {}
DASHBOARD_PORT = int(os.environ.get("CAMERA_DASHBOARD_PORT", "18080"))
MOCK_MEDIA_PORT = int(os.environ.get("CAMERA_MOCK_MEDIA_PORT", "18081"))
PUBLIC_HLS_PORT = int(os.environ.get("CAMERA_PUBLIC_HLS_PORT", "8888"))
PUBLIC_WEBRTC_PORT = int(os.environ.get("CAMERA_PUBLIC_WEBRTC_PORT", "8889"))
OUTPUT_RTSP_BASE = os.environ.get(
    "CAMERA_OUTPUT_RTSP_BASE", "rtsp://127.0.0.1:8554"
).rstrip("/")
STREAM_CAMERA_ORDER = ("DMS", "camera_front", "camera_back", "camera_left", "camera_right")
STREAM_NOMINAL_FPS = {
    "DMS": 10.0,
    "camera_front": 20.0,
    "camera_back": 10.0,
    "camera_left": 10.0,
    "camera_right": 10.0,
}
JETSON_GPU_LOAD_PATHS = (
    Path("/sys/devices/platform/bus@0/17000000.gpu/load"),
    Path("/sys/devices/gpu.0/load"),
)
JETSON_MODEL_PATH = Path("/proc/device-tree/model")
JETSON_THERMAL_ROOT = Path("/sys/devices/virtual/thermal")


def _config_fingerprint() -> tuple[object, ...]:
    try:
        files = sorted(CONFIG_PATH.parent.glob("*.yaml"))
        return (
            id(load_raw_config),
            *(
                (str(path), path.stat().st_mtime_ns, path.stat().st_size)
                for path in files
            ),
        )
    except OSError:
        return (id(load_raw_config), str(CONFIG_PATH), "unavailable")


def _raw_config() -> dict[str, object]:
    """Cache the standalone YAML until its source file changes."""
    global CONFIG_CACHE
    fingerprint = _config_fingerprint()
    with CONFIG_CACHE_LOCK:
        if CONFIG_CACHE is not None and CONFIG_CACHE[0] == fingerprint:
            return CONFIG_CACHE[1]
        try:
            loaded = load_raw_config(CONFIG_PATH)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            if CONFIG_CACHE is not None:
                return CONFIG_CACHE[1]
            return {}
        CONFIG_CACHE = (fingerprint, loaded)
        return loaded


def _status_directory() -> Path:
    status_directory = os.environ.get("CAMERA_STATUS_DIR", "").strip()
    runtime_root = os.environ.get("CAMERA_RUNTIME_ROOT", "").strip()
    if status_directory:
        return Path(status_directory)
    if runtime_root:
        return Path(runtime_root) / "status"
    raw_config = _raw_config()
    runtime = raw_config.get("runtime", {}) or {}
    return Path(str(runtime.get("status_directory", "/opt/ls-vision/data/status")))


def _active_evidence_location() -> tuple[Path, str]:
    runner_status = _runner_status()
    active_runtime = runner_status.get("active_runtime", {}) or {}
    if runner_status.get("fresh") and isinstance(active_runtime, dict):
        directory = active_runtime.get("evidence_directory")
        if directory:
            return Path(str(directory)), str(
                active_runtime.get("evidence_prefix", "snapshots-acceptance")
            )
    raw_config = _raw_config()
    evidence = raw_config.get("evidence", {}) or {}
    return Path(str(evidence.get("directory", ".tmp/ls-vision"))), str(
        evidence.get("prefix", "snapshots-acceptance")
    )


def _latest_evidence_run(root: Path, prefix: str) -> Path | None:
    try:
        root_mtime = root.stat().st_mtime_ns
    except OSError:
        return None
    key = (str(root), prefix)
    with EVIDENCE_RUN_CACHE_LOCK:
        cached = EVIDENCE_RUN_CACHE.get(key)
        if cached is not None and cached[0] == root_mtime:
            return cached[1]
        runs = [path for path in root.glob(f"{prefix}-*") if path.is_dir()]
        latest = max(runs, key=lambda path: path.stat().st_mtime) if runs else None
        EVIDENCE_RUN_CACHE[key] = (root_mtime, latest)
        return latest


def _event_journal_snapshot(
    path: Path,
    *,
    after: int | None,
) -> tuple[int, tuple[tuple[int, str], ...], int]:
    """Read only bytes appended since the previous request.

    Cursor compatibility remains line-based. The cache retains complete lines
    and a bounded set of event IDs while disk reads advance by byte offset.
    """
    try:
        stat = path.stat()
    except OSError:
        return 0, (), 0
    with JOURNAL_CACHE_LOCK:
        state = JOURNAL_CACHE.get(path)
        identity = (int(stat.st_dev), int(stat.st_ino))
        if (
            state is None
            or state.get("identity") != identity
            or int(stat.st_size) < int(state.get("offset", 0))
        ):
            if state is None and len(JOURNAL_CACHE) >= 4:
                JOURNAL_CACHE.pop(next(iter(JOURNAL_CACHE)))
            state = {
                "identity": identity,
                "offset": 0,
                "pending": b"",
                "cursor": 0,
                "lines": deque(maxlen=10_000),
                "event_ids": set(),
            }
            JOURNAL_CACHE[path] = state
        offset = int(state["offset"])
        if int(stat.st_size) > offset:
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read()
                state["offset"] = stream.tell()
            payload = bytes(state["pending"]) + chunk
            parts = payload.split(b"\n")
            state["pending"] = parts.pop() if parts else b""
            lines = state["lines"]
            event_ids = state["event_ids"]
            assert isinstance(lines, deque)
            assert isinstance(event_ids, set)
            for raw_line in parts:
                line = raw_line.decode("utf-8", errors="replace")
                state["cursor"] = int(state["cursor"]) + 1
                lines.append((int(state["cursor"]), line))
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = record.get("event_id") if isinstance(record, dict) else None
                if event_id:
                    event_ids.add(str(event_id))
        lines = state["lines"]
        event_ids = state["event_ids"]
        assert isinstance(lines, deque)
        assert isinstance(event_ids, set)
        cursor = int(state["cursor"])
        selected = (
            ()
            if after is None
            else tuple(item for item in lines if int(item[0]) > max(0, after))
        )
        return cursor, selected, len(event_ids)


def _camera_definitions(stream_host: str = "localhost") -> list[dict[str, object]]:
    try:
        runner_status = _runner_status()
        active_cameras = runner_status.get("active_cameras", [])
        if runner_status.get("fresh") and isinstance(active_cameras, list):
            definitions = []
            for item in active_cameras:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                output_url = str(item.get("dashboard_output") or item.get("output", ""))
                output_path = urlparse(output_url).path.strip("/")
                source = item.get("source")
                media_only = bool(item.get("media_only", False))
                definitions.append(
                    {
                        **item,
                        "media_url": (
                            f"http://{stream_host}:{MOCK_MEDIA_PORT}/"
                            f"{quote(Path(str(source)).name)}"
                            if media_only
                            else None
                        ),
                        "webrtc_url": (
                            f"http://{stream_host}:{PUBLIC_WEBRTC_PORT}/"
                            f"{output_path}/whep"
                        ),
                        "hls_url": (
                            f"http://{stream_host}:{PUBLIC_HLS_PORT}/"
                            f"{output_path}/index.m3u8"
                        ),
                    }
                )
            if definitions:
                return definitions
        raw_config = _raw_config()
        definitions: list[dict[str, object]] = []
        for camera_id in camera_ids(raw_config):
            config = resolve_camera_config(raw_config, camera_id)
            output_url = str(
                config["output"].get("dashboard_rtsp_url")
                or config["output"]["rtsp_url"]
            )
            output_path = urlparse(output_url).path.strip("/")
            source = config["input"].get("mock_video") or config["input"].get("rtsp_url")
            media_only = bool(config["input"].get("media_only", False))
            definitions.append(
                {
                    "id": camera_id,
                    "display_name": next(
                        (
                            str(item.get("display_name"))
                            for item in raw_config.get("cameras", []) or []
                            if str(item.get("id")) == camera_id and item.get("display_name")
                        ),
                        None,
                    ),
                    "source": source,
                    "source_type": config["input"].get("mode", "rtsp"),
                    "media_only": media_only,
                    "mock_sync_group": config["input"].get("mock_sync_group") or None,
                    "mock_sync_period_seconds": config["input"].get(
                        "mock_sync_period_seconds"
                    ),
                    "mock_sync_epoch_seconds": config["input"].get(
                        "mock_sync_epoch_seconds"
                    ),
                    "media_url": (
                        f"http://{stream_host}:{MOCK_MEDIA_PORT}/{quote(Path(str(source)).name)}"
                        if media_only
                        else None
                    ),
                    "output": output_url,
                    "output_video_published": bool(
                        config["output"].get("publish_video", True)
                    ),
                    "webrtc_url": (
                        f"http://{stream_host}:{PUBLIC_WEBRTC_PORT}/{output_path}/whep"
                    ),
                    "hls_url": (
                        f"http://{stream_host}:{PUBLIC_HLS_PORT}/{output_path}/index.m3u8"
                    ),
                    "functions": config.get("functions", {}),
                }
            )
        return definitions
    except (OSError, TypeError, ValueError, KeyError):
        return []


def _proc_cpu() -> tuple[int, int] | None:
    try:
        values = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        numbers = [int(value) for value in values[1:]]
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        return sum(numbers), idle
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _memory() -> dict[str, float | int | None]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0])
    except (FileNotFoundError, ValueError):
        return {"total_mb": None, "used_mb": None, "percent": None}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "percent": round(used * 100 / total, 1) if total else None,
    }


def _processes() -> list[dict[str, int | float | str]]:
    result: list[dict[str, int | str]] = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
            comm = (entry / "comm").read_text(encoding="ascii").strip()
            if comm not in {"python", "python3"}:
                continue
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
            parts = command.split()
            is_worker = "application.camera_runtime" in parts
            is_mock_publisher = any(
                module in parts
                for module in (
                    "adapters.media.gstreamer_mock_publisher",
                    "adapters.media.gstreamer_packet_publisher",
                )
            )
            if not is_worker and not is_mock_publisher:
                continue
            if is_worker:
                if "--config" not in parts:
                    continue
                config_index = parts.index("--config")
                if config_index + 1 >= len(parts) or Path(parts[config_index + 1]) != CONFIG_PATH:
                    continue
            elif "--status-path" in parts:
                status_index = parts.index("--status-path")
                if status_index + 1 >= len(parts):
                    continue
                try:
                    runtime_root = CONFIG_PATH.parents[3]
                    Path(parts[status_index + 1]).relative_to(runtime_root)
                except (IndexError, ValueError):
                    continue
            else:
                continue
            stat = (entry / "stat").read_text(encoding="ascii")
            rest = stat[stat.rfind(")") + 2 :].split()
            ticks = int(rest[11]) + int(rest[12])
            start_ticks = int(rest[19])
            rss_kb = 0
            for line in (entry / "status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
            camera = "unknown"
            run_id = "unknown"
            if "--camera-id" in parts:
                index = parts.index("--camera-id")
                if index + 1 < len(parts):
                    camera = parts[index + 1]
            if "--run-id" in parts:
                index = parts.index("--run-id")
                if index + 1 < len(parts):
                    run_id = parts[index + 1]
            result.append(
                {
                    "pid": pid,
                    "camera": camera,
                    "run_id": run_id,
                    "kind": "vision_worker" if is_worker else "mock_publisher",
                    "ticks": ticks,
                    "start_ticks": start_ticks,
                    "rss_mb": round(rss_kb / 1024, 1),
                }
            )
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    return result


def _runtime_status(camera_id: str) -> dict[str, object]:
    try:
        path = _status_directory() / f"{camera_id}.json"
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _mock_timeline_status() -> dict[str, object]:
    try:
        path = _status_directory() / "mock-timeline.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        updated_at = float(payload.get("updated_at", 0.0) or 0.0)
        payload["fresh"] = 0.0 <= time.time() - updated_at <= 1.0
        payload["ready"] = bool(payload.get("ready") and payload["fresh"])
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _runner_status() -> dict[str, object]:
    try:
        payload = json.loads(
            (_status_directory() / "runner.json").read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            return {}
        updated_at = float(payload.get("updated_at", 0.0) or 0.0)
        payload["fresh"] = 0.0 <= time.time() - updated_at <= 2.0
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _live_metadata() -> dict[str, object]:
    """Read the latest bounded overlay payloads without running GPU metrics."""
    result: dict[str, object] = {"timestamp": time.time(), "cameras": {}}
    try:
        status_dir = _status_directory()
        cameras: dict[str, object] = {}
        for camera in _camera_definitions():
            camera_id = str(camera["id"])
            path = status_dir / f"{camera_id}.metadata.json"
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                cameras[camera_id] = payload
        timeline = _mock_timeline_status()
        for group in (timeline.get("groups", {}) or {}).values():
            if not isinstance(group, dict):
                continue
            for camera_id, publisher in (group.get("cameras", {}) or {}).items():
                camera_payload = cameras.get(str(camera_id))
                if not isinstance(camera_payload, dict) or not isinstance(publisher, dict):
                    continue
                publisher_samples = publisher.get("frame_timing_samples", [])
                if publisher_samples and not camera_payload.get("frame_timing_samples"):
                    camera_payload["frame_timing_samples"] = publisher_samples
        result["cameras"] = cameras
        result["mock_timeline"] = timeline
    except (OSError, TypeError, ValueError):
        pass
    return result


def _evidence_metrics() -> dict[str, object]:
    try:
        root, prefix = _active_evidence_location()
        latest = _latest_evidence_run(root, prefix)
        if latest is None:
            return {"available": False, "run_id": None, "event_count": 0, "root": str(root)}
        events_path = latest / "events.jsonl"
        _cursor, _lines, event_count = _event_journal_snapshot(
            events_path,
            after=None,
        )
        return {
            "available": (latest / "manifest.json").is_file(),
            "run_id": latest.name.removeprefix(f"{prefix}-"),
            "event_count": event_count,
            "root": str(latest),
        }
    except (OSError, TypeError, ValueError):
        return {"available": False, "run_id": None, "event_count": 0}


def _event_feed(after: int = 0, limit: int | None = None) -> dict[str, object]:
    """Return one latest lifecycle snapshot per event ID.

    The evidence journal is append-only, but the dashboard projection is
    keyed by event_id. START creates a row, UPDATE replaces that row, and END
    closes that same row. This keeps lifecycle records out of the request path
    without scanning every event directory.
    """
    try:
        root, prefix = _active_evidence_location()
        latest = _latest_evidence_run(root, prefix)
        if latest is None:
            return {"run_id": None, "cursor": 0, "events": []}
        run_id = latest.name.removeprefix(f"{prefix}-")
        events_path = latest / "events.jsonl"
        if not events_path.is_file():
            return {"run_id": run_id, "cursor": 0, "events": []}

        requested_cursor = max(0, int(after))
        scan_start = requested_cursor
        cursor, lines, _event_count = _event_journal_snapshot(
            events_path,
            after=scan_start,
        )
        latest_by_event: dict[str, tuple[int, dict[str, object]]] = {}
        for sequence, line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            record_type = str(record.get("record_type") or "").upper()
            if record_type not in {"START", "UPDATE", "END"}:
                continue
            event_id = str(record.get("event_id") or "").strip()
            if event_id:
                latest_by_event[event_id] = (sequence, record)

        severity_by_function = {
            "face_recognition": ("event", "Sự kiện"),
            "smoking_behavior": ("warning", "Cảnh báo"),
            "fire_smoke": ("warning", "Cảnh báo"),
            "dms": ("warning", "Cảnh báo"),
        }
        events: list[dict[str, object]] = []
        ordered = sorted(latest_by_event.values(), key=lambda item: item[0])
        if requested_cursor == 0 and limit is not None and limit > 0:
            ordered = ordered[-limit:]
        for sequence, record in ordered:
            record_type = str(record.get("record_type") or "").upper()
            details_value = record.get("details")
            details = dict(details_value) if isinstance(details_value, dict) else {}
            event_path = Path(str(record.get("event_path", "")))
            function = str(record.get("function") or details.get("function") or "event")
            classification = str(
                record.get("classification") or details.get("classification") or function
            )
            identity_value = record.get("identity")
            if identity_value is None:
                identity_value = details.get("identity")
            identity = str(identity_value or "").strip()
            if function == "face_recognition":
                identity = identity or "unknown"
                event_name = f"Nhận diện khuôn mặt: {identity}"
            elif function == "smoking_behavior":
                event_name = "Hành vi hút thuốc"
            elif function == "fire_smoke":
                event_name = "Lửa" if classification == "fire" else "Khói"
            elif function == "dms":
                event_name = f"DMS: {str(record.get('label') or details.get('label') or classification).replace('_', ' ')}"
            else:
                event_name = {
                    "recognized": "Đã nhận diện",
                    "unrecognized": "Không nhận diện được",
                }.get(classification, classification.replace("_", " ").title())

            severity, severity_label = severity_by_function.get(
                function, ("event", "Sự kiện")
            )
            if function == "fire_smoke" and classification == "fire":
                severity, severity_label = "danger", "Nguy hiểm"
            score_value = (
                None
                if function == "face_recognition" and classification == "unrecognized"
                else record.get("score")
                if record.get("score") is not None
                else details.get("score", details.get("last_score"))
            )
            try:
                score = round(float(score_value), 4) if score_value is not None else None
            except (TypeError, ValueError):
                score = None
            timestamp = (
                record.get("timestamp")
                or record.get("updated_at")
                or record.get("ended_at")
                or record.get("started_at")
                or details.get("started_at")
            )
            thumbnail_url = None
            image_url = None
            thumbnail = latest / event_path / "snapshots" / "start-0001-thumbnail.jpg"
            original = latest / event_path / "snapshots" / "start-0001-annotated.jpg"
            if (
                not event_path.is_absolute()
                and ".." not in event_path.parts
                and thumbnail.is_file()
            ):
                thumbnail_url = (
                    "/api/event-thumbnail?run_id="
                    f"{quote(run_id)}&event_path={quote(event_path.as_posix())}&variant=thumbnail"
                )
            if (
                not event_path.is_absolute()
                and ".." not in event_path.parts
                and original.is_file()
            ):
                image_url = (
                    "/api/event-thumbnail?run_id="
                    f"{quote(run_id)}&event_path={quote(event_path.as_posix())}&variant=original"
                )
            lifecycle_state = str(record.get("status") or "active").lower()
            if lifecycle_state not in {"active", "ended"}:
                lifecycle_state = "ended" if record_type == "END" else "active"
            events.append(
                {
                    "sequence": sequence,
                    "event_id": str(record.get("event_id") or details.get("event_id") or sequence),
                    "camera": str(record.get("camera_id") or details.get("camera_id") or "unknown"),
                    "event_name": event_name,
                    "label": record.get("label") or details.get("label") or classification,
                    "name": identity or "unknown",
                    "function": function,
                    "classification": classification,
                    "severity": severity,
                    "severity_label": severity_label,
                    "timestamp": timestamp,
                    "started_at": record.get("started_at"),
                    "updated_at": record.get("updated_at") or timestamp,
                    "ended_at": record.get("ended_at"),
                    "confidence": score,
                    "state": lifecycle_state,
                    "record_type": record_type,
                    "lifecycle": record_type,
                    "region_track_id": details.get("region_track_id"),
                    "confirmation_state": details.get("confirmation_state"),
                    "detector_hits": details.get("detector_hits"),
                    "dynamic_votes": details.get("dynamic_votes"),
                    "dynamic_score": details.get("dynamic_score"),
                    "best_bbox": details.get("best_bbox"),
                    "best_frame_number": details.get("best_frame_number"),
                    "episode_sequence": details.get("episode_sequence"),
                    "person_track_id": details.get("person_track_id"),
                    "positive_votes": details.get("positive_votes"),
                    "observation_window": details.get("observation_window"),
                    "best_score": details.get("best_score"),
                    "best_person_bbox": details.get("best_person_bbox"),
                    "best_model_roi_bbox": details.get("best_model_roi_bbox"),
                    "classifier_score": details.get("classifier_score"),
                    "object_score": details.get("object_score"),
                    "signal_sources": details.get("signal_sources"),
                    "notification_emitted": details.get("notification_emitted"),
                    "notification_min_duration_seconds": details.get(
                        "notification_min_duration_seconds"
                    ),
                    "thumbnail_url": thumbnail_url,
                    "image_url": image_url,
                    "details": details,
                    "start_record": record if record_type == "START" else {},
                }
            )
        return {"run_id": run_id, "cursor": cursor, "events": events}
    except (OSError, TypeError, ValueError):
        return {"run_id": None, "cursor": 0, "events": []}


def _event_thumbnail(
    run_id: str, event_path: str, variant: str = "thumbnail"
) -> Path | None:
    """Resolve only a START thumbnail inside the configured latest run."""
    try:
        root, prefix = _active_evidence_location()
        latest = _latest_evidence_run(root, prefix)
        if latest is not None and latest.name != f"{prefix}-{run_id}":
            latest = None
        relative = Path(event_path)
        if latest is None or relative.is_absolute() or ".." in relative.parts:
            return None
        filename = (
            "start-0001-thumbnail.jpg"
            if variant == "thumbnail"
            else "start-0001-annotated.jpg"
        )
        candidate = (latest / relative / "snapshots" / filename).resolve()
        latest_root = latest.resolve()
        if latest_root not in candidate.parents or not candidate.is_file():
            return None
        return candidate
    except (OSError, TypeError, ValueError):
        return None


def _gpu() -> dict[str, object]:
    global GPU_CACHE
    now = time.monotonic()
    with GPU_LOCK:
        if GPU_CACHE is not None and now - GPU_CACHE[0] < 30.0:
            return dict(GPU_CACHE[1])
        result = _read_gpu()
        GPU_CACHE = (now, result)
        return dict(result)


def _read_gpu() -> dict[str, object]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        row = [value.strip() for value in output.splitlines()[0].split(",")]
        return {
            "available": True,
            "name": row[0],
            "utilization_percent": float(row[1]),
            "memory_used_mb": float(row[2]),
            "memory_total_mb": float(row[3]),
            "temperature_c": float(row[4]),
        }
    except (FileNotFoundError, IndexError, subprocess.SubprocessError, ValueError):
        return _read_jetson_gpu()


def _read_jetson_gpu() -> dict[str, object]:
    try:
        load_path = next(path for path in JETSON_GPU_LOAD_PATHS if path.is_file())
        utilization = float(load_path.read_text(encoding="ascii").strip()) / 10.0
        name = (
            JETSON_MODEL_PATH.read_text(encoding="ascii").replace("\x00", "").strip()
            if JETSON_MODEL_PATH.is_file()
            else "NVIDIA Jetson"
        )
        temperature = None
        for zone in JETSON_THERMAL_ROOT.glob("thermal_zone*"):
            if (zone / "type").read_text(encoding="ascii").strip() != "gpu-thermal":
                continue
            temperature = float((zone / "temp").read_text(encoding="ascii").strip()) / 1000.0
            break
        return {
            "available": True,
            "name": name,
            "utilization_percent": round(utilization, 1),
            "memory_used_mb": None,
            "memory_total_mb": None,
            "temperature_c": round(temperature, 1) if temperature is not None else None,
        }
    except (FileNotFoundError, StopIteration, OSError, ValueError):
        return {"available": False, "name": "unavailable"}


def _collect_metrics(stream_host: str = "localhost") -> dict[str, object]:
    global PREVIOUS_CPU
    now = time.monotonic()
    cpu = _proc_cpu()
    with CPU_LOCK:
        cpu_percent = None
        if cpu is not None and PREVIOUS_CPU is not None:
            total_delta = cpu[0] - PREVIOUS_CPU[0]
            idle_delta = cpu[1] - PREVIOUS_CPU[1]
            if total_delta > 0:
                cpu_percent = round((total_delta - idle_delta) * 100 / total_delta, 1)
        PREVIOUS_CPU = cpu

    hot_processes = _processes()
    processes = [item for item in hot_processes if item.get("kind") == "vision_worker"]
    process_cpu: dict[int, float] = {}
    for process in hot_processes:
        pid = int(process["pid"])
        previous = PREVIOUS_PROCESSES.get(pid)
        if previous is not None:
            tick_delta = int(process["ticks"]) - previous[0]
            elapsed = max(0.001, now - previous[1])
            process_cpu[pid] = max(0.0, tick_delta / CLK_TCK / elapsed * 100)
        PREVIOUS_PROCESSES[pid] = (int(process["ticks"]), now)
    pipeline_cpu_values: list[float] = []
    pipeline_age_values: list[float] = []
    for pipeline in processes:
        pid = int(pipeline["pid"])
        if pid in process_cpu:
            pipeline_cpu_values.append(process_cpu[pid])
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        pipeline_age_values.append(max(0.0, uptime - int(pipeline["start_ticks"]) / CLK_TCK))

    pipeline_cpu = round(sum(pipeline_cpu_values), 1) if pipeline_cpu_values else None
    pipeline_rss = round(sum(float(item["rss_mb"]) for item in processes), 1) if processes else None
    pipeline_age = round(min(pipeline_age_values), 1) if pipeline_age_values else None
    hot_path_cpu_values = [process_cpu[int(item["pid"])] for item in hot_processes if int(item["pid"]) in process_cpu]
    hot_path_cpu = round(sum(hot_path_cpu_values), 1) if hot_path_cpu_values else None
    hot_path_rss = (
        round(sum(float(item["rss_mb"]) for item in hot_processes), 1)
        if hot_processes
        else None
    )

    configured_cameras = _camera_definitions(stream_host)
    mock_timeline = _mock_timeline_status()
    runner_status = _runner_status()
    runner_cameras = runner_status.get("cameras", {}) or {}
    if not isinstance(runner_cameras, dict):
        runner_cameras = {}
    timeline_required = any(camera.get("mock_sync_group") for camera in configured_cameras)
    timeline_ready = bool(mock_timeline.get("ready")) if timeline_required else True
    processes_by_camera = {str(item["camera"]): item for item in processes}
    camera_metrics: list[dict[str, object]] = []
    for camera in configured_cameras:
        camera_id = str(camera["id"])
        process = processes_by_camera.get(camera_id)
        media_only = bool(camera.get("media_only", False))
        media_ready = media_only and Path(str(camera.get("source", ""))).is_file()
        runtime_status = {} if media_only else _runtime_status(camera_id)
        plan_status = runtime_status or runner_cameras.get(camera_id, {}) or {}
        last_frame_at = runtime_status.get("last_frame_at")
        last_output_at = runtime_status.get("last_output_at")
        try:
            last_frame_age = round(max(0.0, time.time() - float(last_frame_at)), 1)
        except (TypeError, ValueError):
            last_frame_age = None
        try:
            output_age = round(max(0.0, time.time() - float(last_output_at)), 1)
        except (TypeError, ValueError):
            output_age = None
        # This is a worker readiness signal only. The browser owns WebRTC
        # playback state; the metrics endpoint must not pretend that a worker
        # heartbeat proves a live frame reached a browser.
        worker_ready = bool(media_ready or (process and output_age is not None and output_age <= 5.0))
        ready = worker_ready
        analysis_debug = runtime_status.get("analysis_debug") or {}
        dms_debug = analysis_debug.get("dms", {}) or {}
        dms_metrics = dms_debug.get("metrics", {}) or {}
        camera_metrics.append(
            {
                **camera,
                "running": bool(media_ready or process is not None),
                "pid": process["pid"] if process else None,
                "run_id": process["run_id"] if process else None,
                "rss_mb": process["rss_mb"] if process else None,
                "worker_ready": worker_ready,
                "ready": ready,
                "last_frame_age_seconds": last_frame_age,
                "last_output_age_seconds": output_age,
                "last_output_pts_ns": runtime_status.get("last_output_pts_ns"),
                "camera_latency_ms": runtime_status.get("camera_latency_ms"),
                "camera_source_timestamp": runtime_status.get("camera_source_timestamp"),
                "camera_latency_source": runtime_status.get(
                    "camera_latency_source", "unavailable"
                ),
                "camera_latency_samples": runtime_status.get("camera_latency_samples", 0),
                "frame_count": runtime_status.get("frame_count"),
                "input_decoder": runtime_status.get("input_decoder"),
                "output_encoder": runtime_status.get("output_encoder"),
                "output_video_published": runtime_status.get(
                    "output_video_published",
                    camera.get("output_video_published", True),
                ),
                "analysis_queue_depth": runtime_status.get("analysis_queue_depth"),
                "analysis_flow": runtime_status.get("analysis_flow", {}),
                "worker_epoch": runtime_status.get("worker_epoch"),
                "analysis_error": runtime_status.get("analysis_error"),
                "config_generation": plan_status.get(
                    "config_generation", runner_status.get("config_generation")
                ),
                "plan_hash": plan_status.get("plan_hash"),
                "enabled_functions": plan_status.get("enabled_functions", []),
                "shared_nodes": plan_status.get("shared_nodes", []),
                "estimated_inference_rate_hz": plan_status.get(
                    "estimated_inference_rate_hz"
                ),
                "model_revisions": plan_status.get("model_revisions", {}),
                "resource_warnings": plan_status.get("resource_warnings", []),
                "smoking_episodes": analysis_debug.get("smoking_episodes", {}),
                "dms": dms_debug,
                "driver_attention": dms_metrics.get("driver_attention", {}),
                "front_assistance": analysis_debug.get("front_assistance", {}),
                "fire_smoke_runtime": analysis_debug.get("fire_smoke_runtime", {}),
            }
        )

    default_stream = next(
        (camera for camera in camera_metrics if camera["id"] == "camera_front"),
        camera_metrics[0] if camera_metrics else {},
    )

    return {
        "timestamp": time.time(),
        "host": {"cpu_percent": cpu_percent, "cpu_cores": os.cpu_count(), "memory": _memory()},
        "gpu": _gpu(),
        "pipeline": {
            "running": bool(processes),
            "pid": processes[0]["pid"] if processes else None,
            "pids": [item["pid"] for item in processes],
            "camera_count": len(camera_metrics),
            "ready": bool(camera_metrics)
            and all(bool(camera["ready"]) for camera in camera_metrics)
            and timeline_ready,
            "cameras": [str(item["id"]) for item in camera_metrics],
            "cpu_percent": pipeline_cpu,
            "rss_mb": pipeline_rss,
            "hot_path_cpu_percent": hot_path_cpu,
            "hot_path_rss_mb": hot_path_rss,
            "hot_path_processes": [
                {
                    "pid": item["pid"],
                    "camera": item["camera"],
                    "kind": item["kind"],
                    "cpu_percent": (
                        round(process_cpu[int(item["pid"])], 1)
                        if int(item["pid"]) in process_cpu
                        else None
                    ),
                    "rss_mb": item["rss_mb"],
                }
                for item in hot_processes
            ],
            "age_seconds": pipeline_age,
            "camera_details": camera_metrics,
            "mock_timeline": mock_timeline,
            "config_generation": runner_status.get("config_generation"),
            "config_reload_error": runner_status.get("reload_error"),
            "last_restarted_cameras": runner_status.get(
                "last_restarted_cameras", []
            ),
        },
        "stream": {
            "worker_ready": any(bool(camera["worker_ready"]) for camera in camera_metrics),
            "webrtc_url": default_stream.get("webrtc_url"),
            "hls_url": default_stream.get("hls_url"),
        },
        "evidence": _evidence_metrics(),
    }


def collect_metrics(stream_host: str = "localhost") -> dict[str, object]:
    now = time.monotonic()
    with METRICS_LOCK:
        cached = METRICS_CACHE.get(stream_host)
        if cached is not None and now - cached[0] < 2.5:
            return cached[1]
        result = _collect_metrics(stream_host)
        METRICS_CACHE[stream_host] = (now, result)
        return result


def _rtsp_probe_target(path: str) -> tuple[str, int, str]:
    parsed = urlparse(OUTPUT_RTSP_BASE)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 554)
    normalized_path = f"/{path.strip('/')}"
    return host, port, f"rtsp://{host}:{port}{normalized_path}"


def _probe_rtsp_path(path: str) -> dict[str, object]:
    """Probe MediaMTX itself so configured paths are not reported as published."""
    now = time.monotonic()
    with STREAM_PROBE_LOCK:
        cached = STREAM_PROBE_CACHE.get(path)
        if cached is not None and now - cached[0] < 1.0:
            return cached[1]

    host, port, url = _rtsp_probe_target(path)
    result: dict[str, object] = {"published": False, "codec": None}
    request = (
        f"DESCRIBE {url} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: ls-vision-stream-contract/1\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((host, port), timeout=0.35) as connection:
            connection.settimeout(0.35)
            connection.sendall(request)
            response = connection.recv(16384).decode("utf-8", errors="replace")
        status_line = response.splitlines()[0] if response else ""
        published = " 200 " in f" {status_line} " and "m=video" in response
        upper_response = response.upper()
        codec = "h264" if "H264/90000" in upper_response else None
        result = {"published": published, "codec": codec}
    except OSError:
        pass

    with STREAM_PROBE_LOCK:
        STREAM_PROBE_CACHE[path] = (now, result)
    return result


def _configured_stream_contract() -> list[dict[str, object]]:
    """Build the public stream contract from canonical camera output config."""
    raw_config = _raw_config()
    streams: list[dict[str, object]] = []
    for camera_id in STREAM_CAMERA_ORDER:
        config = resolve_camera_config(raw_config, camera_id)
        input_config = config["input"]
        output_config = config["output"]
        media_only = bool(input_config.get("media_only", False))
        output_url = str(output_config["rtsp_url"])
        output_path = f"/{urlparse(output_url).path.strip('/')}"
        streams.append(
            {
                "camera_id": camera_id,
                "role": "media_only_passthrough" if media_only else "vision_processed",
                "rtsp_path": output_path,
                "width": int(output_config.get("width", input_config["width"])),
                "height": int(output_config.get("height", input_config["height"])),
                "nominal_fps": float(
                    output_config.get("rate_hz") or STREAM_NOMINAL_FPS[camera_id]
                ),
                "sync_group": input_config.get("mock_sync_group") or None,
                "pipeline_output_enabled": media_only
                or bool(output_config.get("publish_video", True)),
                "source_camera_id": camera_id,
                "alias_of": None,
            }
        )

    back = next(item for item in streams if item["camera_id"] == "camera_back")
    streams.append(
        {
            **back,
            "camera_id": "camera_cargo",
            "source_camera_id": "camera_back",
            "alias_of": "camera_back",
        }
    )
    return streams


def _stream_manifest(stream_host: str = "localhost") -> dict[str, object]:
    runner = _runner_status()
    timeline = _mock_timeline_status()
    surround = (timeline.get("groups", {}) or {}).get("vehicle_surround", {}) or {}
    surround_locked = bool(timeline.get("ready") and surround.get("locked"))
    parsed_base = urlparse(OUTPUT_RTSP_BASE)
    rtsp_port = int(parsed_base.port or 554)
    generation = int(runner.get("config_generation", 0) or 0)
    # Jetson production currently runs Python 3.10, before datetime.UTC exists.
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: UP017
    streams: list[dict[str, object]] = []

    for ordinal, contract in enumerate(_configured_stream_contract(), start=1):
        camera_id = str(contract["camera_id"])
        path = str(contract["rtsp_path"])
        sync_group = contract.get("sync_group")
        probe = _probe_rtsp_path(path)
        published = bool(probe.get("published"))
        codec = probe.get("codec")
        if not contract["pipeline_output_enabled"]:
            state = "DISABLED"
            published = False
            codec = None
        elif not published:
            state = "OFFLINE"
        elif codec != "h264" or (sync_group == "vehicle_surround" and not surround_locked):
            state = "DEGRADED"
        else:
            state = "READY"
        streams.append(
            {
                "camera_id": camera_id,
                "ordinal": ordinal,
                "role": contract["role"],
                "rtsp_path": path,
                "state": state,
                "published": published,
                "codec": codec,
                "width": contract["width"],
                "height": contract["height"],
                "nominal_fps": contract["nominal_fps"],
                "clock_rate": 90000 if codec == "h264" else None,
                "sync_group": sync_group,
                "source_camera_id": contract["source_camera_id"],
                "alias_of": contract["alias_of"],
                "source_epoch": None,
                "last_packet_at": None,
            }
        )

    return {
        "schema": "letron.vision.stream-manifest/v1",
        "generation": generation,
        "generated_at": generated_at,
        "media_base": {
            "local_rtsp": OUTPUT_RTSP_BASE,
            "lan_rtsp": f"rtsp://{stream_host}:{rtsp_port}",
        },
        "streams": streams,
    }


def _stream_host_from_header(host_header: str) -> str:
    """Use the browser-facing host instead of hard-coding Jetson localhost."""
    host = host_header.strip()
    if not host:
        return "localhost"
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class DashboardHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health/live":
            payload = b'{"status":"live"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/health/ready":
            metrics = collect_metrics(_stream_host_from_header(self.headers.get("Host", "")))
            ready = bool((metrics.get("pipeline") or {}).get("ready"))
            payload = json.dumps({"status": "ready" if ready else "starting"}).encode("utf-8")
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/metrics":
            stream_host = _stream_host_from_header(self.headers.get("Host", ""))
            payload = json.dumps(collect_metrics(stream_host)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/v1/streams":
            stream_host = _stream_host_from_header(self.headers.get("Host", ""))
            payload = json.dumps(_stream_manifest(stream_host)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/live-metadata":
            payload = json.dumps(_live_metadata()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path.startswith("/api/events/"):
            parts = [item for item in path.split("/") if item]
            query = parse_qs(parsed.query)
            event_id = parts[2] if len(parts) >= 3 else ""
            feed = _event_feed()
            event = next((item for item in feed.get("events", []) if item.get("event_id") == event_id), None)
            if event is None:
                self.send_error(404)
                return
            if len(parts) == 3:
                payload = json.dumps(event).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            variant = "thumbnail" if parts[3] == "thumbnail" else "original"
            start_record = event.get("start_record", {}) or {}
            run_id = str(query.get("run_id", [start_record.get("run_id", "")])[0])
            event_path = str(query.get("event_path", [start_record.get("event_path", "")])[0])
            thumbnail = _event_thumbnail(run_id, event_path, variant)
            if thumbnail is None:
                self.send_error(404)
                return
            payload = thumbnail.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/events":
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            try:
                limit = int(query.get("limit", ["0"])[0])
            except ValueError:
                limit = 0
            payload = json.dumps(_event_feed(after, limit if limit > 0 else None)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/event-thumbnail":
            query = parse_qs(parsed.query)
            run_id = query.get("run_id", [""])[0]
            event_path = query.get("event_path", [""])[0]
            variant = query.get("variant", ["thumbnail"])[0]
            thumbnail = _event_thumbnail(run_id, event_path, variant)
            if thumbnail is None:
                self.send_error(404)
                return
            payload = thumbnail.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            # Event evidence paths are immutable after START.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            stat = thumbnail.stat()
            self.send_header("ETag", f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"')
            self.send_header("Last-Modified", formatdate(stat.st_mtime, usegmt=True))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    bind_host = os.environ.get("CAMERA_DASHBOARD_HOST", "127.0.0.1")
    ThreadingHTTPServer((bind_host, DASHBOARD_PORT), DashboardHandler).serve_forever()


if __name__ == "__main__":
    main()
