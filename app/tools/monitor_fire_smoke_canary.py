#!/usr/bin/env python3
"""Collect bounded native-WSL fire/smoke canary evidence."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen


def _get_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=10) as response:  # noqa: S310 - operator-selected localhost endpoint
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", default="camera_safety")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--duration-hours", type=float, default=8.0)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--controlled-fire-report", type=Path)
    parser.add_argument("--report", type=Path, default=Path(".tmp/fire-smoke-canary/report.json"))
    args = parser.parse_args()
    if args.duration_hours < 0 or args.interval_seconds <= 0:
        raise SystemExit("duration must be non-negative and interval must be positive")

    deadline = time.monotonic() + args.duration_hours * 3600.0
    samples: list[dict[str, object]] = []
    event_ids: set[str] = set()
    duplicate_event_ids: set[str] = set()
    error: str | None = None
    while True:
        try:
            metrics = _get_json(f"{args.base_url.rstrip('/')}/api/metrics")
            events = _get_json(f"{args.base_url.rstrip('/')}/api/events?limit=1000")
            cameras = ((metrics.get("pipeline") or {}) if isinstance(metrics.get("pipeline"), dict) else {}).get("camera_details", [])
            camera = next((item for item in cameras if isinstance(item, dict) and item.get("id") == args.camera), {})
            event_rows = events.get("events", []) if isinstance(events, dict) else []
            batch_event_ids: set[str] = set()
            for event in event_rows if isinstance(event_rows, list) else []:
                if not isinstance(event, dict) or event.get("camera") != args.camera:
                    continue
                event_id = str(event.get("event_id", ""))
                if event_id in batch_event_ids:
                    duplicate_event_ids.add(event_id)
                batch_event_ids.add(event_id)
                event_ids.add(event_id)
            samples.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "model": (camera.get("fire_smoke_runtime", {}) if isinstance(camera, dict) else {}),
                    "analysis_flow": (camera.get("analysis_flow", {}) if isinstance(camera, dict) else {}),
                    "ready": camera.get("ready") if isinstance(camera, dict) else False,
                    "event_count": len(event_ids),
                }
            )
        except Exception as exc:  # retain evidence and mark the gate instead of hiding a gap
            error = type(exc).__name__
            samples.append({"at": datetime.now(UTC).isoformat(), "error": error})
        if time.monotonic() >= deadline:
            break
        time.sleep(min(args.interval_seconds, max(0.0, deadline - time.monotonic())))

    controlled: dict[str, object] | None = None
    if args.controlled_fire_report and args.controlled_fire_report.is_file():
        raw = json.loads(args.controlled_fire_report.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            controlled = raw
    duration_seconds = args.duration_hours * 3600.0
    gates = {
        "stable_for_8_hours": duration_seconds >= 8 * 3600,
        "target_camera_observed": any("model" in sample for sample in samples),
        "no_stale_or_out_of_order_results": all(
            not int((sample.get("analysis_flow") or {}).get("stale_drops", 0))
            and not int((sample.get("analysis_flow") or {}).get("out_of_order_drops", 0))
            for sample in samples
            if isinstance(sample.get("analysis_flow"), dict)
        ),
        "no_duplicate_events": not duplicate_event_ids,
        "controlled_true_fire_latency_le_3s": bool(
            controlled and float(controlled.get("latency_seconds", 999.0)) <= 3.0
        ),
        "false_alarms_per_hour_measured": False,
        "runtime_errors_absent": error is None,
    }
    report = {
        "schema_version": 1,
        "started_at": samples[0].get("at") if samples else None,
        "finished_at": datetime.now(UTC).isoformat(),
        "camera": args.camera,
        "duration_seconds": duration_seconds,
        "samples": samples,
        "duplicate_event_ids": sorted(duplicate_event_ids),
        "controlled_fire": controlled,
        "gates": gates,
        "accepted": all(gates.values()),
        "status": "accepted" if all(gates.values()) else "not_accepted",
        "note": "False alarms/hour requires labeled negative CCTV hours and is intentionally fail-closed here.",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.report), "accepted": report["accepted"]}))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
