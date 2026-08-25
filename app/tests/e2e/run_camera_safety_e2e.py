"""Real Compose E2E for the new Camera Safety runtime.

The runner owns only the ``ls-vision`` Compose project. It records a
report and returns non-zero when any production gate is missing; a timeout or
container existence is never treated as acceptance.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

APP_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = APP_ROOT / "deploy" / "docker" / "compose.yaml"
E2E_COMPOSE = APP_ROOT / "deploy" / "docker" / "compose.e2e.yaml"


def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "-f", str(E2E_COMPOSE), *args],
        cwd=APP_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def _http_json(url: str, timeout: float = 3.0) -> dict[str, object] | None:
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed localhost E2E URLs
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, URLError, ValueError):
        return None


def _wait_http(url: str, timeout: int) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = _http_json(url)
        if payload is not None:
            return payload
        time.sleep(1)
    return None


def _wait_hls(url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3.0) as response:  # noqa: S310 - dashboard-provided localhost URL
                if response.status == 200 and b"#EXTM3U" in response.read(256):
                    return True
        except (OSError, URLError):
            pass
        time.sleep(1)
    return False


def _camera_metrics(payload: dict[str, object]) -> list[dict[str, object]]:
    pipeline = payload.get("pipeline", {})
    cameras = pipeline.get("camera_details", []) if isinstance(pipeline, dict) else []
    return [item for item in cameras if isinstance(item, dict)]


def _camera_key(item: dict[str, object]) -> str:
    """Return the stable camera identifier used by both worker and API payloads."""
    return str(item.get("camera") or item.get("id") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--wait", type=int, default=120)
    parser.add_argument("--build", action="store_true", help="build the image before startup")
    parser.add_argument(
        "--report",
        type=Path,
        default=APP_ROOT.parent / ".tmp" / "ls-vision-e2e" / "summary.json",
    )
    parser.add_argument("--keep", action="store_true", help="keep containers after the run")
    args = parser.parse_args()
    report: dict[str, object] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "accepted": False,
        "gates": {},
        "errors": [],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def fail(message: str) -> None:
        report["errors"].append(message)

    if shutil.which("docker") is None:
        fail("docker CLI is unavailable")
        report["status"] = "blocked"
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 2

    config = _run("config", "--quiet")
    report["gates"]["compose_config"] = config.returncode == 0
    if config.returncode:
        fail(config.stderr.strip() or "docker compose config failed")
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    start_args = ["up", "-d", "--remove-orphans"]
    if args.build:
        start_args.insert(2, "--build")
    started = _run(*start_args, timeout=900)
    report["gates"]["compose_started"] = started.returncode == 0
    if started.returncode:
        fail(started.stderr.strip() or "ls-vision Compose start failed")
    try:
        live = _wait_http("http://127.0.0.1:18080/health/live", args.wait)
        ready = _wait_http("http://127.0.0.1:18080/health/ready", args.wait)
        report["gates"]["dashboard_live"] = live is not None
        report["gates"]["dashboard_ready"] = bool(ready and ready.get("status") == "ready")
        metrics = _http_json("http://127.0.0.1:18080/api/metrics") or {}
        cameras = _camera_metrics(metrics)
        expected = {"camera_face", "camera_safety", "DMS"}
        report["gates"]["one_worker_per_camera"] = {_camera_key(item) for item in cameras} == expected
        report["gates"]["all_cameras_ready"] = bool(cameras) and all(item.get("ready") for item in cameras)
        hls_urls = [str(item["hls_url"]) for item in cameras if item.get("hls_url")]
        report["gates"]["mediamtx_hls_output"] = bool(hls_urls) and all(
            _wait_hls(url, args.wait) for url in hls_urls
        )
        before = {_camera_key(item): item.get("frame_count") for item in cameras}
        time.sleep(max(1, args.duration))
        after_payload = _http_json("http://127.0.0.1:18080/api/metrics") or {}
        after = _camera_metrics(after_payload)
        report["gates"]["input_output_fresh"] = bool(after) and all(
            item.get("last_frame_age_seconds") is not None
            and item.get("last_output_age_seconds") is not None
            and float(item.get("last_frame_age_seconds", 999)) <= 5
            and float(item.get("last_output_age_seconds", 999)) <= 5
            and int(item.get("frame_count", 0)) > int(before.get(_camera_key(item), -1) or -1)
            for item in after
        )
        events = _http_json("http://127.0.0.1:18080/api/events") or {}
        report["gates"]["event_feed_start_only"] = all(
            item.get("record_type") == "START" for item in events.get("events", []) if isinstance(item, dict)
        )
        restarted = _run("restart", "ls-vision", timeout=120)
        report["gates"]["container_restart"] = restarted.returncode == 0
        report["gates"]["state_api_after_restart"] = _wait_http(
            "http://127.0.0.1:18080/health/live", args.wait
        ) is not None
        report["gates"]["evidence_api_after_restart"] = _http_json(
            "http://127.0.0.1:18080/api/events"
        ) is not None
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        fail(f"runtime gate failed: {error}")
    finally:
        if not args.keep:
            _run("down", "--remove-orphans", timeout=120)

    report["accepted"] = not report["errors"] and all(
        bool(value) for value in report["gates"].values()
    )
    report["status"] = "accepted" if report["accepted"] else "failed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
