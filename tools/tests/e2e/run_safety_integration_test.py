"""Real Frigate-core integration matrix for smoking-only Camera Safety.

The runner uses the launcher, Docker services, the real smoking ONNX artifact,
and the real smoker replay. It never fabricates Event IDs or detector output.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "deploy" / "run.ps1"
CONFIG = ROOT / "deploy" / "config.yaml"
DEFAULT_CONFIG = ROOT / "deploy" / "config.yaml"
API_URL = "http://127.0.0.1:5001"
CAMERA = "safety_camera"
LABEL = "smoking"
SUB_LABEL = "camera-safety"
REPLAY_CONTAINER = "camera-replay-safety-camera"
TEST_START_EPOCH = 0.0


def _run_launcher(
    command: str,
    config: Path,
    timeout: int = 180,
) -> None:
    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LAUNCHER),
        "-Command",
        command,
        "-ConfigFile",
        str(config),
    ]
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"launcher {command} failed:\n{result.stdout}\n{result.stderr}")


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], cwd=ROOT, text=True, capture_output=True, timeout=timeout
    )


def _docker_json(*args: str, timeout: int = 30) -> Any:
    result = _docker(*args, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _container_running(name: str) -> bool:
    result = _docker("inspect", name, "--format", "{{.State.Running}}")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _container_health(name: str) -> str:
    result = _docker("inspect", name, "--format", "{{.State.Health.Status}}")
    return result.stdout.strip() if result.returncode == 0 else "missing"


def _safety_health() -> dict[str, Any]:
    result = _docker("exec", "camera-safety", "cat", "/tmp/camera-safety-health.json")
    if result.returncode:
        return {"ready": False, "error": result.stderr.strip()}
    return json.loads(result.stdout)


def _request(path: str, *, params: dict[str, Any] | None = None) -> requests.Response:
    return requests.get(f"{API_URL}{path}", params=params, timeout=10)


def _events(*, in_progress: int | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "camera": CAMERA,
        "label": LABEL,
        "sub_label": SUB_LABEL,
        "limit": 100,
    }
    if in_progress is not None:
        params["in_progress"] = in_progress
    response = _request("/api/events", params=params)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else payload.get("events", [])


def _event(event_id: str) -> dict[str, Any]:
    response = _request(f"/api/events/{event_id}")
    response.raise_for_status()
    return response.json()


def _sqlite_rows(query: str, *values: str) -> list[dict[str, Any]]:
    code = """
import glob, json, sqlite3, sys
paths = ['/config/frigate.infrastructure.db']
paths = [path for path in paths if __import__('os').path.isfile(path)] or glob.glob('/config/*.db')
if not paths:
    raise SystemExit('no Frigate SQLite database found')
db = sqlite3.connect(paths[0])
db.row_factory = sqlite3.Row
rows = db.execute(sys.argv[1], sys.argv[2:]).fetchall()
print(json.dumps([dict(row) for row in rows], default=str))
"""
    return _docker_json("exec", "frigate", "python3", "-c", code, query, *values)


def _wait_for(
    predicate: Callable[[], Any], timeout: int, description: str, interval: float = 1.0
) -> Any:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # polling intentionally tolerates startup races
            last_error = str(exc)
        time.sleep(interval)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise RuntimeError(f"timeout waiting for {description}{suffix}")


def _assert_latest_frame() -> dict[str, Any]:
    response = _request(f"/api/{CAMERA}/latest.jpg")
    response.raise_for_status()
    frame_time = float(response.headers.get("X-Frame-Time", "0"))
    if frame_time <= 0 or not response.content:
        raise RuntimeError("latest frame did not contain a positive X-Frame-Time and bytes")
    return {"status": response.status_code, "frame_time": frame_time, "bytes": len(response.content)}


def _latest_completed() -> dict[str, Any]:
    events = [
        event
        for event in _events()
        if float(event.get("start_time", 0)) >= TEST_START_EPOCH - 10
        and (event.get("end_time") or event.get("duration"))
    ]
    if not events:
        raise RuntimeError("no completed camera-safety smoking Event")
    return events[0]


def _case(report: dict[str, Any], name: str, fn: Callable[[], dict[str, Any]]) -> None:
    started = time.monotonic()
    try:
        details = fn()
        report["cases"][name] = {"passed": True, "details": details}
    except Exception as exc:
        report["cases"][name] = {"passed": False, "error": str(exc)}
        report["errors"].append(f"{name}: {exc}")
    finally:
        report["cases"][name]["duration_seconds"] = round(time.monotonic() - started, 2)


def _case_startup_and_source() -> dict[str, Any]:
    health = _wait_for(
        lambda: _safety_health() if _container_health("camera-safety") == "healthy" else None,
        90,
        "Safety healthy state",
    )
    frame = _wait_for(_assert_latest_frame, 60, "Frigate latest live frame")
    if not _container_running("frigate") or _container_health("frigate") != "healthy":
        raise RuntimeError("Frigate was not healthy alongside Safety")
    return {"safety_health": health, "latest_frame": frame}


def _case_event_lifecycle() -> dict[str, Any]:
    event = _wait_for(_latest_completed, 180, "completed real smoking Event", 2.0)
    event_id = str(event["id"])
    db_rows = _wait_for(
        lambda: _sqlite_rows(
            "SELECT id,camera,label,sub_label,start_time,end_time,has_clip,has_snapshot "
            "FROM event WHERE id=?",
            event_id,
        ),
        30,
        f"SQLite Event row {event_id}",
    )
    if len(db_rows) != 1:
        raise RuntimeError(f"SQLite did not contain exactly one row for Event {event_id}")
    db_event = db_rows[0]
    for key in ("camera", "label", "sub_label"):
        if db_event[key] != event.get(key):
            raise RuntimeError(f"API/SQLite mismatch for {key}: {event.get(key)!r} != {db_event[key]!r}")
    if not event.get("end_time") or not event.get("has_clip") or not event.get("has_snapshot"):
        raise RuntimeError(f"completed Event lacks lifecycle/media fields: {event}")
    return {"event": event, "sqlite": db_event}


def _case_media_and_review() -> dict[str, Any]:
    event = _latest_completed()
    event_id = str(event["id"])
    media: dict[str, Any] = {}
    for suffix in ("snapshot-clean.webp", "clip.mp4"):
        deadline = time.monotonic() + 60
        last_status = 0
        last_bytes = 0
        while time.monotonic() < deadline:
            response = _request(f"/api/events/{event_id}/{suffix}")
            last_status = response.status_code
            last_bytes = len(response.content)
            if response.status_code == 200 and response.content:
                media[suffix] = {
                    "available": True,
                    "status": response.status_code,
                    "bytes": last_bytes,
                }
                break
            time.sleep(2)
        else:
            media[suffix] = {
                "available": False,
                "status": last_status,
                "bytes": last_bytes,
            }

    review: dict[str, Any]
    try:
        review_response = _request(
            "/api/review",
            params={"cameras": CAMERA, "labels": "all", "limit": 100},
        )
        review_payload = review_response.json()
        review_match = None
        if isinstance(review_payload, list):
            for candidate in review_payload:
                data = candidate.get("data") or {}
                detections = data.get("detections") or []
                objects = data.get("objects") or []
                if event_id in detections and any(
                    str(value).split(":", 1)[0] == LABEL for value in objects
                ):
                    review_match = candidate
                    break
        review = {
            "status": review_response.status_code,
            "is_list": isinstance(review_payload, list),
            "count": len(review_payload) if isinstance(review_payload, list) else 0,
            "matched_event_id": bool(review_match),
        }
    except Exception as exc:
        review = {"status": 0, "is_list": False, "error": str(exc)}

    failures = [
        f"{suffix}: {details}"
        for suffix, details in media.items()
        if not details.get("available")
    ]
    if (
        review.get("status") != 200
        or not review.get("is_list")
        or not review.get("matched_event_id")
    ):
        failures.append(f"review: {review}")
    details = {"event_id": event_id, "media": media, "review": review}
    if failures:
        raise RuntimeError(f"media/review integration failures: {'; '.join(failures)}; details={details}")
    return details


def _case_restart_reconcile() -> dict[str, Any]:
    active = _wait_for(
        lambda: (_events(in_progress=1) or [None])[0],
        90,
        "an active Safety Event before restart",
        1.0,
    )
    active_id = str(active["id"])
    result = _docker("restart", "camera-safety", timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    _wait_for(
        lambda: _safety_health() if _container_health("camera-safety") == "healthy" else None,
        90,
        "Safety healthy after restart",
    )
    reconciled = _wait_for(
        lambda: _event(active_id) if _event(active_id).get("end_time") else None,
        30,
        f"reconciled Event {active_id}",
    )
    open_events = _events(in_progress=1)
    if len(open_events) > 1:
        raise RuntimeError(f"duplicate open Safety Events after reconcile: {open_events}")
    return {"restarted_event_id": active_id, "reconciled_event": reconciled, "open_count": len(open_events)}


def _case_source_disconnect_and_recovery() -> dict[str, Any]:
    stopped = _docker("stop", "--time", "2", REPLAY_CONTAINER, timeout=30)
    if stopped.returncode:
        raise RuntimeError(stopped.stderr.strip())
    time.sleep(18)
    degraded = _safety_health()
    if degraded.get("ready"):
        raise RuntimeError(f"Safety stayed ready after replay source disconnect: {degraded}")
    if _container_health("frigate") != "healthy":
        raise RuntimeError("Frigate became unhealthy when replay source disconnected")
    started = _docker("start", REPLAY_CONTAINER, timeout=30)
    if started.returncode:
        raise RuntimeError(started.stderr.strip())
    recovered = _wait_for(
        lambda: _safety_health() if _safety_health().get("ready") else None,
        90,
        "Safety source recovery",
    )
    return {"degraded_health": degraded, "recovered_health": recovered}


def _case_frigate_restart_and_no_duplicates() -> dict[str, Any]:
    restarted = _docker("restart", "frigate", timeout=90)
    if restarted.returncode:
        raise RuntimeError(restarted.stderr.strip())
    _wait_for(
        lambda: True if _container_health("frigate") == "healthy" else None,
        120,
        "Frigate healthy after restart",
    )
    _wait_for(_assert_latest_frame, 60, "latest frame after Frigate restart")
    _wait_for(
        lambda: _safety_health() if _safety_health().get("ready") else None,
        90,
        "Safety healthy after Frigate restart",
    )
    open_events = _events(in_progress=1)
    if len(open_events) > 1:
        raise RuntimeError(f"duplicate open Safety Events after Frigate restart: {open_events}")
    return {"frigate_health": _container_health("frigate"), "open_count": len(open_events)}


def main() -> int:
    global TEST_START_EPOCH
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "safety-integration")
    args = parser.parse_args()
    run_dir = args.output / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "config": str(CONFIG.relative_to(ROOT)),
        "cases": {},
        "errors": [],
        "runtime_restored": False,
        "accepted": False,
    }
    TEST_START_EPOCH = time.time()
    started = False
    try:
        _run_launcher("acceptance-start", CONFIG)
        started = True
        _case(report, "startup_and_latest_frame", _case_startup_and_source)
        _case(report, "event_lifecycle_api_sqlite", _case_event_lifecycle)
        _case(report, "event_media_and_review_api", _case_media_and_review)
        _case(report, "safety_restart_reconcile", _case_restart_reconcile)
        _case(report, "source_disconnect_recovery", _case_source_disconnect_and_recovery)
        _case(report, "frigate_restart_no_duplicates", _case_frigate_restart_and_no_duplicates)
    except Exception as exc:
        report["errors"].append(f"setup: {exc}")
    finally:
        if started:
            try:
                _run_launcher("acceptance-park", CONFIG, timeout=60)
            except Exception as exc:
                report["errors"].append(f"cleanup acceptance-park: {exc}")
            try:
                _run_launcher("stop", CONFIG, timeout=120)
            except Exception as exc:
                report["errors"].append(f"cleanup safety stop: {exc}")
            try:
                _run_launcher("start", DEFAULT_CONFIG, timeout=180)
                report["runtime_restored"] = True
            except Exception as exc:
                report["errors"].append(f"cleanup default restore: {exc}")
        report["finished_at"] = datetime.now(UTC).isoformat()
        passed = bool(report["cases"]) and all(case["passed"] for case in report["cases"].values())
        report["accepted"] = passed and not report["errors"] and report["runtime_restored"]
        (run_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
