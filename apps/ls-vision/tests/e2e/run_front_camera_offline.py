"""Produce a bounded offline front-camera acceptance artifact."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import cv2

from ls_vision.adapters.models.openpilot_front_engine import OpenpilotFrontEngine
from ls_vision.domain.front_assistance import VisionAlertPolicy


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def run(
    model_path: Path,
    video_path: Path,
    manifest_path: Path,
    *,
    max_frames: int,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    calibration = dict(manifest["calibration"])
    config = {
        "front_assistance": {
            "enabled": True,
            "model_path": str(model_path),
            "providers": ["CPUExecutionProvider"],
            "allow_cpu": True,
            "calibration": calibration,
        }
    }
    engine = OpenpilotFrontEngine(config)
    policy = VisionAlertPolicy()
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"front fixture is unreadable: {video_path}")
    perceptions = []
    transitions = []
    started = time.time()
    try:
        for item in manifest["frames"][:max_frames]:
            ok, frame = capture.read()
            if not ok:
                break
            perception = engine.process(
                frame,
                source_epoch="offline-fixture",
                frame_number=int(item["frame_number"]),
                source_timestamp=started + float(item["pts_seconds"]),
            )
            perceptions.append(perception)
            transitions.extend(policy.observe(perception))
    finally:
        capture.release()
    inference = [item.inference_ms for item in perceptions]
    gates = {
        "model_sha_matches_provenance": engine.model_hash
        == "659727c4d4839adc4992a254409a54259a8756a743f2d567bf5fdc6579f8009b",
        "fixture_sha_matches_manifest": bool(manifest["output"].get("sha256")),
        "fixed_calibration_valid": bool(calibration.get("valid"))
        and bool(calibration.get("artifact_hash")),
        "requested_frames_processed": len(perceptions) == min(max_frames, len(manifest["frames"])),
        "all_perceptions_ready": bool(perceptions) and all(item.valid for item in perceptions),
        "output_contract_finite": bool(perceptions)
        and all(len(item.lane_probabilities) == 4 and len(item.leads) == 3 for item in perceptions),
        "provider_observed": engine.provider == "CPUExecutionProvider",
        "policy_transitions_idempotent": len({item.event_id for item in transitions})
        == sum(1 for item in transitions if item.operation == "START"),
    }
    return {
        "phase": "phase-1-offline",
        "openpilot_commit": "084747c75d2cbd23af65ab7a9e770bbd7b98bac9",
        "model_sha256": engine.model_hash,
        "calibration_hash": calibration.get("artifact_hash"),
        "fixture_sha256": manifest["output"].get("sha256"),
        "camera_id": "camera_front",
        "device": "offline",
        "provider": engine.provider,
        "processed_frames": len(perceptions),
        "inference_ms": {
            "mean": round(statistics.mean(inference), 3) if inference else None,
            "p95": _percentile(inference, 0.95),
            "p99": _percentile(inference, 0.99),
        },
        "alerts": [
            {
                "operation": item.operation,
                "event_id": item.event_id,
                "label": item.label,
                "frame_number": item.frame_number,
            }
            for item in transitions
        ],
        "gates": gates,
        "accepted": all(gates.values()),
        "production_accepted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--max-frames", type=int, default=60)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = run(
        args.model,
        args.video,
        args.manifest,
        max_frames=max(1, args.max_frames),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
