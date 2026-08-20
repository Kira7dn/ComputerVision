from __future__ import annotations

import json
import os
import subprocess
import time
from email.utils import formatdate
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, urlparse

from config import camera_ids, load_raw_config, resolve_camera_config

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
CLK_TCK = os.sysconf("SC_CLK_TCK")
CPU_LOCK = Lock()
PREVIOUS_CPU: tuple[int, int] | None = None
PREVIOUS_PROCESSES: dict[int, tuple[int, float]] = {}
DASHBOARD_PORT = 18080


def _camera_definitions() -> list[dict[str, object]]:
    try:
        raw_config = load_raw_config(CONFIG_PATH)
        definitions: list[dict[str, object]] = []
        for camera_id in camera_ids(raw_config):
            config = resolve_camera_config(raw_config, camera_id)
            output_url = str(config["output"]["rtsp_url"])
            output_path = urlparse(output_url).path.strip("/")
            definitions.append(
                {
                    "id": camera_id,
                    "source": config["input"].get("mock_video") or config["input"].get("rtsp_url"),
                    "output": output_url,
                    "webrtc_url": f"http://localhost:8889/{output_path}/whep",
                    "hls_url": f"http://localhost:8888/{output_path}/index.m3u8",
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


def _processes() -> list[dict[str, int | str]]:
    result: list[dict[str, int | str]] = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
            comm = (entry / "comm").read_text(encoding="ascii").strip()
            if comm not in {"python", "python3"}:
                continue
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
            parts = command.split()
            if not any(part.endswith("/deepstream_safety/pipeline.py") for part in parts):
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
            result.append({"pid": pid, "camera": camera, "run_id": run_id, "ticks": ticks, "start_ticks": start_ticks, "rss_mb": round(rss_kb / 1024, 1)})
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    return result


def _runtime_status(camera_id: str) -> dict[str, object]:
    try:
        raw_config = load_raw_config(CONFIG_PATH)
        runtime = raw_config.get("runtime", {}) or {}
        status_dir = Path(str(runtime.get("status_directory", "/opt/camera-deepstream/status")))
        path = status_dir / f"{camera_id}.json"
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _live_metadata() -> dict[str, object]:
    """Read the latest bounded overlay payloads without running GPU metrics."""
    result: dict[str, object] = {"timestamp": time.time(), "cameras": {}}
    try:
        raw_config = load_raw_config(CONFIG_PATH)
        runtime = raw_config.get("runtime", {}) or {}
        status_dir = Path(str(runtime.get("status_directory", "/opt/camera-deepstream/status")))
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
        result["cameras"] = cameras
    except (OSError, TypeError, ValueError):
        pass
    return result


def _evidence_metrics() -> dict[str, object]:
    try:
        raw_config = load_raw_config(CONFIG_PATH)
        evidence = raw_config.get("evidence", {}) or {}
        root = Path(str(evidence.get("directory", ".tmp/deepstream-safety")))
        prefix = str(evidence.get("prefix", "snapshots-acceptance"))
        runs = [path for path in root.glob(f"{prefix}-*") if path.is_dir()]
        if not runs:
            return {"available": False, "run_id": None, "event_count": 0, "root": str(root)}
        latest = max(runs, key=lambda path: path.stat().st_mtime)
        events_path = latest / "events.jsonl"
        event_ids: set[str] = set()
        if events_path.is_file():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = record.get("event_id")
                if event_id:
                    event_ids.add(str(event_id))
        return {
            "available": (latest / "manifest.json").is_file(),
            "run_id": latest.name.removeprefix(f"{prefix}-"),
            "event_count": len(event_ids),
            "root": str(latest),
        }
    except (OSError, TypeError, ValueError):
        return {"available": False, "run_id": None, "event_count": 0}


def _event_feed(after: int = 0, limit: int | None = None) -> dict[str, object]:
    """Return one dashboard record per event start.

    END records remain internal evidence for lifecycle closure, but are not a
    dashboard event and never become a user notification.
    """
    try:
        raw_config = load_raw_config(CONFIG_PATH)
        evidence = raw_config.get("evidence", {}) or {}
        root = Path(str(evidence.get("directory", ".tmp/deepstream-safety")))
        prefix = str(evidence.get("prefix", "snapshots-acceptance"))
        runs = [path for path in root.glob(f"{prefix}-*") if path.is_dir()]
        if not runs:
            return {"run_id": None, "cursor": 0, "events": []}
        latest = max(runs, key=lambda path: path.stat().st_mtime)
        run_id = latest.name.removeprefix(f"{prefix}-")
        events_path = latest / "events.jsonl"
        if not events_path.is_file():
            return {"run_id": run_id, "cursor": 0, "events": []}

        lines = events_path.read_text(encoding="utf-8").splitlines()
        start = max(0, int(after))
        if start == 0 and limit is not None and limit > 0:
            start = max(0, len(lines) - limit)
        events: list[dict[str, object]] = []
        severity_by_function = {
            "face_recognition": ("event", "Sự kiện"),
            "smoking_behavior": ("warning", "Cảnh báo"),
            "fire_smoke": ("warning", "Cảnh báo"),
        }
        for sequence, line in enumerate(lines[start:], start=start + 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record_type = str(record.get("record_type") or "").upper()
            if record_type != "START":
                continue

            event_file: Path | None = None
            event_path = Path(str(record.get("event_path", "")))
            if event_path and not event_path.is_absolute() and ".." not in event_path.parts:
                candidate = latest / event_path / "event.json"
                if candidate.is_file():
                    event_file = candidate
            details: dict[str, object] = {}
            if event_file is not None:
                try:
                    loaded = json.loads(event_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        details = loaded
                except (OSError, json.JSONDecodeError):
                    pass

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
                else record.get("score", details.get("last_score"))
            )
            try:
                score = round(float(score_value), 4) if score_value is not None else None
            except (TypeError, ValueError):
                score = None
            timestamp = record.get("started_at") or details.get("started_at")
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
            events.append(
                {
                    "sequence": sequence,
                    "event_id": str(record.get("event_id") or details.get("event_id") or sequence),
                    "camera": str(record.get("camera_id") or details.get("camera_id") or "unknown"),
                    "event_name": event_name,
                    "name": identity or "unknown",
                    "function": function,
                    "classification": classification,
                    "severity": severity,
                    "severity_label": severity_label,
                    "timestamp": timestamp,
                    "confidence": score,
                    "state": "started",
                    "record_type": record_type,
                    "thumbnail_url": thumbnail_url,
                    "image_url": image_url,
                    "details": details,
                    "start_record": record,
                }
            )
        return {"run_id": run_id, "cursor": len(lines), "events": events}
    except (OSError, TypeError, ValueError):
        return {"run_id": None, "cursor": 0, "events": []}


def _event_thumbnail(
    run_id: str, event_path: str, variant: str = "thumbnail"
) -> Path | None:
    """Resolve only a START thumbnail inside the configured latest run."""
    try:
        raw_config = load_raw_config(CONFIG_PATH)
        evidence = raw_config.get("evidence", {}) or {}
        root = Path(str(evidence.get("directory", ".tmp/deepstream-safety")))
        prefix = str(evidence.get("prefix", "snapshots-acceptance"))
        latest = next(
            (
                path
                for path in root.glob(f"{prefix}-*")
                if path.is_dir() and path.name == f"{prefix}-{run_id}"
            ),
            None,
        )
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
        return {"available": False, "name": "unavailable"}


def collect_metrics() -> dict[str, object]:
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

    processes = _processes()
    pipeline_cpu_values: list[float] = []
    pipeline_age_values: list[float] = []
    for pipeline in processes:
        pid = int(pipeline["pid"])
        previous = PREVIOUS_PROCESSES.get(pid)
        if previous is not None:
            tick_delta = int(pipeline["ticks"]) - previous[0]
            elapsed = max(0.001, now - previous[1])
            pipeline_cpu_values.append(tick_delta / CLK_TCK / elapsed * 100)
        PREVIOUS_PROCESSES[pid] = (int(pipeline["ticks"]), now)
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        pipeline_age_values.append(max(0.0, uptime - int(pipeline["start_ticks"]) / CLK_TCK))

    pipeline_cpu = round(sum(pipeline_cpu_values), 1) if pipeline_cpu_values else None
    pipeline_rss = round(sum(float(item["rss_mb"]) for item in processes), 1) if processes else None
    pipeline_age = round(min(pipeline_age_values), 1) if pipeline_age_values else None

    configured_cameras = _camera_definitions()
    processes_by_camera = {str(item["camera"]): item for item in processes}
    camera_metrics: list[dict[str, object]] = []
    for camera in configured_cameras:
        camera_id = str(camera["id"])
        process = processes_by_camera.get(camera_id)
        runtime_status = _runtime_status(camera_id)
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
        worker_ready = bool(process and output_age is not None and output_age <= 5.0)
        ready = worker_ready
        camera_metrics.append(
            {
                **camera,
                "running": process is not None,
                "pid": process["pid"] if process else None,
                "run_id": process["run_id"] if process else None,
                "rss_mb": process["rss_mb"] if process else None,
                "worker_ready": worker_ready,
                "ready": ready,
                "last_frame_age_seconds": last_frame_age,
                "last_output_age_seconds": output_age,
                "last_output_pts_ns": runtime_status.get("last_output_pts_ns"),
                "frame_count": runtime_status.get("frame_count"),
                "analysis_queue_depth": runtime_status.get("analysis_queue_depth"),
                "worker_epoch": runtime_status.get("worker_epoch"),
                "analysis_error": runtime_status.get("analysis_error"),
            }
        )

    default_stream = next(
        (camera for camera in camera_metrics if camera["id"] == "camera_safety"),
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
            "camera_count": len(processes),
            "ready": bool(camera_metrics) and all(bool(camera["ready"]) for camera in camera_metrics),
            "cameras": [item["camera"] for item in processes],
            "cpu_percent": pipeline_cpu,
            "rss_mb": pipeline_rss,
            "age_seconds": pipeline_age,
            "camera_details": camera_metrics,
        },
        "stream": {
            "worker_ready": any(bool(camera["worker_ready"]) for camera in camera_metrics),
            "webrtc_url": default_stream.get("webrtc_url"),
            "hls_url": default_stream.get("hls_url"),
        },
        "evidence": _evidence_metrics(),
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        if path == "/api/metrics":
            payload = json.dumps(collect_metrics()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever()
