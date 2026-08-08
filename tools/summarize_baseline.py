#!/usr/bin/env python3
"""Merge bounded two-camera baseline evidence and enforce runtime gates."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def runtime_resources() -> dict[str, Any]:
    docker = subprocess.run(
        ["docker", "stats", "frigate", "--no-stream", "--format", "{{json .}}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    docker_stats = json.loads(docker.stdout)
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    utilization, used, total = [
        float(part.strip()) for part in gpu.stdout.splitlines()[0].split(",")
    ]
    return {
        "cpu_percent": float(docker_stats["CPUPerc"].rstrip("%")),
        "memory": docker_stats["MemUsage"],
        "gpu_percent": utilization,
        "vram_used_mib": used,
        "vram_total_mib": total,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--face-known", type=Path, required=True)
    parser.add_argument("--face-unknown", type=Path, required=True)
    parser.add_argument("--lpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    fixture = load(args.fixture)
    known = load(args.face_known)
    unknown = load(args.face_unknown)
    lpr = load(args.lpr)
    face_reports = [known, unknown]
    positive_events = [
        event
        for report in face_reports
        for event in report["ground_truth"]["events"]
        if event["identity"] != "unknown"
    ]
    latencies = [
        float(event["capture_to_recognition_ms"])
        for event in positive_events
        if event["capture_to_recognition_ms"] is not None
    ]
    durations = {
        "face_known": float(known["duration_seconds"]),
        "face_unknown": float(unknown["duration_seconds"]),
        "lpr": float(lpr["monitor"]["duration_seconds"]),
    }
    gates = {
        "all_test_cases_under_120_seconds": all(value < 120 for value in durations.values()),
        "face_known_runtime": bool(known.get("accepted")),
        "face_unknown_runtime": bool(unknown.get("accepted")),
        "face_candidate_correlation": bool(positive_events)
        and all(event["candidate_correlation"] for event in positive_events),
        "face_pending_zero": all(
            report["face_metrics"][-1].get("pending_count") == 0
            for report in face_reports
        ),
        "lpr_runtime": bool(lpr.get("passed")),
        "ground_truth_present": all(
            report.get("ground_truth") is not None for report in (*face_reports, lpr)
        ),
    }
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": "two-camera-baseline",
        "status": "DONE" if all(gates.values()) else "FAILED",
        "fixture": fixture,
        "test_case_duration_seconds": durations,
        "gates": gates,
        "ground_truth": {
            "face_passage_detection_recall": statistics.mean(
                report["ground_truth"]["passage_detection_recall"]
                for report in face_reports
            ),
            "face_recognition_precision": statistics.mean(
                report["ground_truth"]["recognition_precision"]
                for report in face_reports
            ),
            "face_recognition_recall": statistics.mean(
                report["ground_truth"]["recognition_recall"]
                for report in face_reports
            ),
            "face_capture_to_recognition_ms_p50": (
                statistics.median(latencies) if latencies else None
            ),
            "face_capture_to_recognition_ms_p95": (
                float(np.percentile(latencies, 95)) if latencies else None
            ),
            "lpr_passage_detection_recall": lpr["ground_truth"][
                "passage_detection_recall"
            ],
            "lpr_exact_match": lpr["ground_truth"]["lpr_exact_match"],
            "lpr_readable_denominator": lpr["ground_truth"][
                "readable_denominator"
            ],
        },
        "latency_gates_ms": {
            "detector_inference_max": max(
                report["detector_inference_ms_max"] for report in face_reports
            ),
            "face_first_attempt_max": max(
                report["face_metric_max"]["first_attempt_ms_max"]
                for report in face_reports
            ),
            "face_confirmed_max": max(
                report["face_metric_max"]["confirmed_ms_max"]
                for report in face_reports
            ),
            "face_embedding_p95": max(
                report["face_metric_max"]["embedding_ms_p95"]
                for report in face_reports
            ),
        },
        "calls_per_second": {
            "face_recognition": max(
                report["calls_per_second"]["face_recognition"]
                for report in face_reports
            ),
            "plate_recognition": max(
                float(sample["embeddings"].get("plate_recognition", 0))
                for sample in lpr["stability"]["samples"]
            ),
            "plate_detection": max(
                float(sample["embeddings"].get("yolov9_plate_detection", 0))
                for sample in lpr["stability"]["samples"]
            ),
        },
        "resources": runtime_resources(),
        "evidence": {
            "face_known": str(args.face_known.resolve()),
            "face_unknown": str(args.face_unknown.resolve()),
            "lpr": str(args.lpr.resolve()),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(args.output), "status": summary["status"], "gates": gates}, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
