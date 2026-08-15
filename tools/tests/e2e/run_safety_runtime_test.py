"""Smoking-only Safety runtime acceptance runner.

This intentionally uses the launcher and real ONNX/MP4 artifacts. It does not
manufacture Frigate Event IDs or fake detections.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "deploy" / "run.ps1"
CONFIG = ROOT / "deploy" / "config.safety-replay-smoker.yaml"
DEFAULT_CONFIG = ROOT / "deploy" / "config.yaml"
SAFETY_CONFIG = ROOT / "deploy" / "safety.yaml"


def _run_launcher(command: str, config: Path, safety: Path | None = SAFETY_CONFIG, timeout: int = 180) -> None:
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCHER), "-Command", command, "-ConfigFile", str(config)]
    if safety is not None:
        args += ["-SafetyConfigFile", str(safety)]
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"launcher {command} failed:\n{result.stdout}\n{result.stderr}")


def _events(api_url: str) -> list[dict]:
    response = requests.get(f"{api_url}/api/events", params={"camera": "safety_camera", "label": "smoking", "limit": 100}, timeout=5)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else payload.get("events", [])


def _resource_snapshot() -> list[dict]:
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}", "frigate", "camera-safety"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    if result.returncode:
        return []
    snapshots = []
    for line in result.stdout.splitlines():
        name, cpu, memory = (line.split("|", 2) + ["", "", ""])[:3]
        snapshots.append({"name": name, "cpu": cpu, "memory": memory})
    return snapshots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:5001")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "safety-runtime")
    args = parser.parse_args()
    run_dir = args.output / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "started_at": datetime.now(UTC).isoformat(), "accepted": False, "measurement_valid": False, "runtime_restored": False, "events": [], "errors": []}
    try:
        _run_launcher("acceptance-start", CONFIG)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            events = _events(args.api_url)
            safety_events = [event for event in events if event.get("sub_label") == "camera-safety"]
            report["events"] = safety_events
            if safety_events and any(event.get("end_time") or event.get("duration") for event in safety_events):
                report["measurement_valid"] = True
                report["lifecycle"] = {
                    "event_count": len(safety_events),
                    "has_clip": all(bool(event.get("has_clip")) for event in safety_events),
                    "has_snapshot": all(bool(event.get("has_snapshot")) for event in safety_events),
                }
                report["resources"] = _resource_snapshot()
                break
            time.sleep(1)
        if not report["measurement_valid"]:
            raise RuntimeError("no completed camera-safety smoking Event observed before timeout")
        _run_launcher("acceptance-park", CONFIG, timeout=60)
        _run_launcher("stop", CONFIG, SAFETY_CONFIG, timeout=120)
        _run_launcher("start", DEFAULT_CONFIG, None, timeout=180)
        report["runtime_restored"] = True
        report["accepted"] = True
    except Exception as exc:
        report["errors"].append(str(exc))
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        (run_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
