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
    parser.add_argument("--max-pipeline-cpu-percent", type=float, default=500.0)
    parser.add_argument("--max-pipeline-rss-mb", type=float, default=3500.0)
    parser.add_argument("--max-camera-latency-ms", type=float, default=500.0)
    parser.add_argument("--max-analysis-queue-depth", type=int, default=1)
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
    vision_cameras = [item for item in cameras if not item.get("media_only")]
    pipeline_cpu = pipeline.get("cpu_percent") if isinstance(pipeline, dict) else None
    pipeline_rss = pipeline.get("rss_mb") if isinstance(pipeline, dict) else None
    camera_latencies = {
        str(item.get("id")): item.get("camera_latency_ms") for item in vision_cameras
    }
    queue_depths = {
        str(item.get("id")): item.get("analysis_queue_depth") for item in vision_cameras
    }
    report["performance"] = {
        "pipeline_cpu_percent": pipeline_cpu,
        "pipeline_rss_mb": pipeline_rss,
        "camera_latency_ms": camera_latencies,
        "analysis_queue_depth": queue_depths,
        "limits": {
            "pipeline_cpu_percent": args.max_pipeline_cpu_percent,
            "pipeline_rss_mb": args.max_pipeline_rss_mb,
            "camera_latency_ms": args.max_camera_latency_ms,
            "analysis_queue_depth": args.max_analysis_queue_depth,
        },
    }
    gates["runtime_plans_observable"] = bool(cameras) and all(
        item.get("plan_hash")
        and isinstance(item.get("enabled_functions"), list)
        and isinstance(item.get("shared_nodes"), list)
        and isinstance(item.get("estimated_inference_rate_hz"), int | float)
        and isinstance(item.get("model_revisions"), dict)
        and all(
            item["model_revisions"].get(function)
            for function in item.get("enabled_functions", [])
        )
        for item in cameras
    )
    gates["runner_config_accepted"] = bool(
        isinstance(pipeline, dict)
        and int(pipeline.get("config_generation", 0) or 0) >= 1
        and not pipeline.get("config_reload_error")
    )
    gates["resource_budget_clear"] = bool(cameras) and all(
        not item.get("resource_warnings") for item in cameras
    )
    gates["analysis_no_backlog"] = bool(vision_cameras) and all(
        isinstance(depth, int) and depth <= args.max_analysis_queue_depth
        for depth in queue_depths.values()
    )
    gates["performance_budget"] = bool(
        isinstance(pipeline_cpu, int | float)
        and pipeline_cpu <= args.max_pipeline_cpu_percent
        and isinstance(pipeline_rss, int | float)
        and pipeline_rss <= args.max_pipeline_rss_mb
        and camera_latencies
        and all(
            isinstance(latency, int | float)
            and 0.0 <= latency <= args.max_camera_latency_ms
            for latency in camera_latencies.values()
        )
    )
    for gate_name in (
        "runtime_plans_observable",
        "runner_config_accepted",
        "resource_budget_clear",
        "analysis_no_backlog",
        "performance_budget",
    ):
        if not gates[gate_name]:
            errors.append(f"production gate failed: {gate_name}")
    mock_timeline = pipeline.get("mock_timeline", {}) if isinstance(pipeline, dict) else {}
    groups = mock_timeline.get("groups", {}) if isinstance(mock_timeline, dict) else {}
    surround = groups.get("vehicle_surround", {}) if isinstance(groups, dict) else {}
    timeline_cameras = surround.get("cameras", {}) if isinstance(surround, dict) else {}
    gates["mock_timeline_ready"] = bool(
        mock_timeline.get("ready")
        and surround.get("locked")
        and set(timeline_cameras) == {
            "camera_front",
            "camera_back",
            "camera_left",
            "camera_right",
        }
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
