"""Native Jetson production acceptance for the source-based LS-Vision release."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / ".tmp" / "ls-vision-native-e2e" / "summary.json"
EXPECTED_CAMERAS = {"DMS", "camera_front", "camera_back", "camera_left", "camera_right"}


def _request_json(url: str, *, host: str = "vision.local") -> dict[str, object] | None:
    try:
        request = Request(url, headers={"Host": host})
        with urlopen(request, timeout=5) as response:  # noqa: S310 - operator-selected target
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, URLError, ValueError):
        return None


def _wait_json(url: str, timeout: int) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = _request_json(url)
        if payload is not None:
            return payload
        time.sleep(1)
    return None


def _ssh(alias: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", alias, command], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def _ssh_sudo(alias: str, command: str, password: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", alias, f"sudo -S -p '' {command}"],
        input=f"{password}\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jetson-alias", default="jetson-nano")
    parser.add_argument("--base-url", default="http://vision.local")
    parser.add_argument("--wait", type=int, default=120)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    report: dict[str, object] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "accepted": False,
        "gates": {},
        "errors": [],
    }
    gates = report["gates"]
    errors = report["errors"]
    assert isinstance(gates, dict) and isinstance(errors, list)

    service = _ssh(
        args.jetson_alias,
        "systemctl is-active ls-vision.service mediamtx.service ls-vision-ingress.service",
    )
    gates["native_services_active"] = service.returncode == 0
    if service.returncode:
        errors.append(service.stderr.strip() or service.stdout.strip())

    manifest = _ssh(
        args.jetson_alias,
        "test -f /opt/ls-vision/current/release-manifest.json && cat /opt/ls-vision/current/release-manifest.json",
    )
    gates["source_release_manifest"] = manifest.returncode == 0
    if manifest.returncode == 0:
        try:
            report["release"] = json.loads(manifest.stdout)
        except ValueError:
            gates["source_release_manifest"] = False

    live = _wait_json(f"{args.base_url.rstrip('/')}/health/live", args.wait)
    ready = _wait_json(f"{args.base_url.rstrip('/')}/health/ready", args.wait)
    gates["dashboard_live"] = bool(live and live.get("status") == "live")
    gates["dashboard_ready"] = bool(ready and ready.get("status") == "ready")

    metrics = _request_json(f"{args.base_url.rstrip('/')}/api/metrics") or {}
    pipeline = metrics.get("pipeline", {})
    details = pipeline.get("camera_details", []) if isinstance(pipeline, dict) else []
    cameras = [item for item in details if isinstance(item, dict)]
    gates["camera_topology"] = {str(item.get("id")) for item in cameras} == EXPECTED_CAMERAS
    gates["all_camera_contracts_ready"] = bool(cameras) and all(item.get("ready") for item in cameras)
    gates["media_contracts_published"] = bool(cameras) and all(
        item.get("media_url") if item.get("media_only") else item.get("hls_url") for item in cameras
    )

    if args.restart:
        sudo_password = os.environ.get("LS_VISION_SUDO_PASSWORD", "")
        restarted = (
            _ssh_sudo(args.jetson_alias, "systemctl restart ls-vision.service", sudo_password)
            if sudo_password
            else _ssh(args.jetson_alias, "sudo -n systemctl restart ls-vision.service")
        )
        gates["service_restart"] = restarted.returncode == 0 and bool(
            _wait_json(f"{args.base_url.rstrip('/')}/health/ready", args.wait)
        )
        if restarted.returncode:
            errors.append(restarted.stderr.strip() or restarted.stdout.strip())
    else:
        gates["service_restart"] = None

    required = [value for value in gates.values() if value is not None]
    report["accepted"] = bool(required) and all(required) and not errors
    report["completed_at"] = datetime.now(UTC).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
