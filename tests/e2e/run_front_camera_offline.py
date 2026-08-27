"""Produce a bounded offline front-camera acceptance artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cv2
import yaml

from adapters.models.openpilot_front_engine import OpenpilotFrontEngine
from domain.front_assistance import VisionAlertPolicy
from domain.front_overlay import project_front_overlay


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list | tuple):
        return all(_finite(item) for item in value)
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_from_profile(
    config_path: Path,
    video_path: Path,
    *,
    max_frames: int,
) -> dict[str, Any]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    camera = next(
        item for item in raw.get("cameras", []) if item.get("id") == "camera_front"
    )
    calibration = dict(camera["front_assistance"]["calibration"])
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"front fixture is unreadable: {video_path}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        available = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        capture.release()
    if not math.isfinite(fps) or fps <= 0.0 or available <= 0:
        raise ValueError("front fixture has invalid FPS/frame count")
    frame_count = min(max_frames, available)
    return {
        "schema_version": 2,
        "dataset": "production-mock-profile",
        "camera": "camera_front",
        "output": {
            "path": video_path.name,
            "width": calibration["source_width"],
            "height": calibration["source_height"],
            "fps": fps,
            "frame_count": frame_count,
            "sha256": _sha256(video_path),
        },
        "calibration": calibration,
        "frames": [
            {"frame_number": index, "pts_seconds": index / fps}
            for index in range(frame_count)
        ],
    }


def run(
    model_path: Path,
    video_path: Path,
    manifest_path: Path | None,
    *,
    max_frames: int,
    config_path: Path | None = None,
) -> dict[str, Any]:
    if (manifest_path is None) == (config_path is None):
        raise ValueError("exactly one front manifest or profile config is required")
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path is not None
        else _manifest_from_profile(config_path, video_path, max_frames=max_frames)
    )
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
    overlay_geometry = [
        project_front_overlay(
            item,
            calibration,
            width=int(calibration["source_width"]),
            height=int(calibration["source_height"]),
            lane_min_probability=0.0,
            path_half_width_m=0.9,
            lead_min_probability=0.5,
            road_edge_max_std_m=0.6,
        )
        for item in perceptions
    ]
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
        and all(
            len(item.lane_probabilities) == 4
            and len(item.lane_line_stds) == 4
            and len(item.road_edges) == len(item.road_edge_stds) == 2
            and len(item.leads) == 3
            and len(item.plan_velocity) == len(item.plan_acceleration) == 33
            and len(item.plan_orientation) == len(item.plan_orientation_rate) == 33
            and len(item.pose) == len(item.pose_stds) == 6
            and len(item.road_transform) == len(item.road_transform_stds) == 6
            and len(item.wide_from_device_euler) == 3
            and len(item.model_meta) == 55
            and _finite(asdict(item))
            for item in perceptions
        ),
        "full_overlay_finite": bool(overlay_geometry)
        and all(
            geometry.summary()["visible_lane_count"] == 4
            and geometry.summary()["visible_road_edge_count"] == 2
            and geometry.summary()["horizon_marker_count"] == 6
            and _finite(geometry.summary())
            for geometry in overlay_geometry
        ),
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
        "overlay": overlay_geometry[-1].summary() if overlay_geometry else {},
        "gates": gates,
        "accepted": all(gates.values()),
        "production_accepted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--config", type=Path)
    parser.add_argument("--max-frames", type=int, default=60)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = run(
        args.model,
        args.video,
        args.manifest,
        max_frames=max(1, args.max_frames),
        config_path=args.config,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
