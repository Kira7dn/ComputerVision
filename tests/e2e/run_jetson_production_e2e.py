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


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _camera_map(metrics: dict[str, object]) -> dict[str, dict[str, object]]:
    pipeline = metrics.get("pipeline", {}) or {}
    details = pipeline.get("camera_details", []) if isinstance(pipeline, dict) else []
    return {
        str(item.get("id")): item
        for item in details
        if isinstance(item, dict) and item.get("id")
    }


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
    parser.add_argument("--max-hot-path-cpu-percent", type=float, default=500.0)
    parser.add_argument("--max-camera-latency-ms", type=float, default=500.0)
    parser.add_argument("--max-analysis-queue-depth", type=int, default=1)
    parser.add_argument("--measurement-seconds", type=float, default=60.0)
    parser.add_argument("--measurement-interval-seconds", type=float, default=2.0)
    parser.add_argument("--min-cadence-ratio", type=float, default=0.90)
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

    metrics_url = f"{args.base_url.rstrip('/')}/api/metrics"
    samples: list[tuple[float, dict[str, object]]] = []
    measurement_started = time.monotonic()
    while True:
        sampled = _request_json(metrics_url)
        if sampled is not None:
            samples.append((time.monotonic(), sampled))
        elapsed = time.monotonic() - measurement_started
        if elapsed >= max(0.0, args.measurement_seconds):
            break
        time.sleep(min(max(0.1, args.measurement_interval_seconds), args.measurement_seconds - elapsed))
    metrics = samples[-1][1] if samples else {}
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
    pipeline_cpu_samples: list[float] = []
    hot_path_cpu_samples: list[float] = []
    for _sampled_at, sample in samples:
        sample_pipeline = sample.get("pipeline", {}) or {}
        if not isinstance(sample_pipeline, dict):
            continue
        sample_pipeline_cpu = sample_pipeline.get("cpu_percent")
        sample_hot_path_cpu = sample_pipeline.get("hot_path_cpu_percent")
        if isinstance(sample_pipeline_cpu, int | float):
            pipeline_cpu_samples.append(float(sample_pipeline_cpu))
        if isinstance(sample_hot_path_cpu, int | float):
            hot_path_cpu_samples.append(float(sample_hot_path_cpu))
    pipeline_cpu_p95 = _percentile(pipeline_cpu_samples, 0.95)
    hot_path_cpu_p95 = _percentile(hot_path_cpu_samples, 0.95)
    latency_cameras = [
        item
        for item in vision_cameras
        if item.get("camera_latency_source") not in {None, "unavailable"}
    ]
    camera_latencies = {
        str(item.get("id")): item.get("camera_latency_ms")
        for item in latency_cameras
    }
    queue_depths = {
        str(item.get("id")): item.get("analysis_queue_depth") for item in vision_cameras
    }
    cadence: dict[str, dict[str, float | int | None]] = {}
    if len(samples) >= 2:
        first_at, first_metrics = samples[0]
        last_at, last_metrics = samples[-1]
        duration = max(0.001, last_at - first_at)
        first_cameras = _camera_map(first_metrics)
        last_cameras = _camera_map(last_metrics)
        for camera_id, last_camera in last_cameras.items():
            if bool(last_camera.get("media_only")):
                continue
            first_camera = first_cameras.get(camera_id, {})
            first_functions = ((first_camera.get("analysis_flow") or {}).get("functions") or {})
            last_functions = ((last_camera.get("analysis_flow") or {}).get("functions") or {})
            if not isinstance(first_functions, dict) or not isinstance(last_functions, dict):
                continue
            planned_rate = sum(
                float(status.get("planned_rate_hz", 0.0) or 0.0)
                for status in last_functions.values()
                if isinstance(status, dict)
            )
            processed_delta = sum(
                max(
                    0,
                    int(status.get("processed", 0) or 0)
                    - int((first_functions.get(name, {}) or {}).get("processed", 0) or 0),
                )
                for name, status in last_functions.items()
                if isinstance(status, dict)
            )
            processed_rate = processed_delta / duration
            cadence[camera_id] = {
                "duration_seconds": round(duration, 3),
                "planned_rate_hz": round(planned_rate, 3),
                "processed_rate_hz": round(processed_rate, 3),
                "ratio": round(processed_rate / planned_rate, 4) if planned_rate else None,
            }
    report["performance"] = {
        "pipeline_cpu_percent": pipeline_cpu,
        "pipeline_cpu_p50_percent": _percentile(pipeline_cpu_samples, 0.50),
        "pipeline_cpu_p95_percent": pipeline_cpu_p95,
        "hot_path_cpu_p50_percent": _percentile(hot_path_cpu_samples, 0.50),
        "hot_path_cpu_p95_percent": hot_path_cpu_p95,
        "pipeline_rss_mb": pipeline_rss,
        "camera_latency_ms": camera_latencies,
        "analysis_queue_depth": queue_depths,
        "cadence": cadence,
        "sample_count": len(samples),
        "limits": {
            "pipeline_cpu_percent": args.max_pipeline_cpu_percent,
            "hot_path_cpu_percent": args.max_hot_path_cpu_percent,
            "pipeline_rss_mb": args.max_pipeline_rss_mb,
            "camera_latency_ms": args.max_camera_latency_ms,
            "analysis_queue_depth": args.max_analysis_queue_depth,
            "min_cadence_ratio": args.min_cadence_ratio,
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
    gates["analysis_cadence"] = bool(cadence) and all(
        isinstance(item.get("ratio"), int | float)
        and float(item["ratio"]) >= args.min_cadence_ratio
        for item in cadence.values()
    )
    gates["performance_budget"] = bool(
        isinstance(pipeline_cpu_p95, int | float)
        and pipeline_cpu_p95 <= args.max_pipeline_cpu_percent
        and isinstance(hot_path_cpu_p95, int | float)
        and hot_path_cpu_p95 <= args.max_hot_path_cpu_percent
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
        "analysis_cadence",
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
