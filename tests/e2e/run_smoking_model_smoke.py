#!/usr/bin/env python3
"""Load the complete smoking ensemble and run one frame through its real providers."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

APP_ROOT = Path(__file__).resolve().parents[2] / "apps"
if str(APP_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(APP_ROOT / "src"))

from adapters.models.smoking_engine import SmokingBehaviorEngine  # noqa: E402
from bootstrap.config import load_raw_config, resolve_camera_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--camera", default="camera_safety")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    config = resolve_camera_config(load_raw_config(args.config), args.camera)
    if args.cpu:
        config["smoking_behavior"]["providers"] = ["CPUExecutionProvider"]
        config["smoking_behavior"]["require_gpu_provider"] = False
    capture = cv2.VideoCapture(str(args.video))
    ok, frame = capture.read()
    capture.release()
    if not ok or frame is None:
        raise RuntimeError(f"unable to read fixture frame: {args.video}")
    height, width = frame.shape[:2]

    started = time.perf_counter()
    engine = SmokingBehaviorEngine(config)
    loaded = time.perf_counter()
    inference_times: list[float] = []
    batch = None
    for frame_number in range(1, max(1, args.iterations) + 1):
        inference_started = time.perf_counter()
        batch = engine.process(
            frame,
            [(1, 0.0, 0.0, float(width), float(height))],
            frame_number,
        )
        inference_times.append((time.perf_counter() - inference_started) * 1000.0)
    assert batch is not None
    observation = batch.observations[0]
    report = {
        "accepted": len(batch.observations) == 1,
        "load_seconds": round(loaded - started, 3),
        "inference_ms": [round(value, 2) for value in inference_times],
        "steady_inference_ms": round(
            sum(inference_times[1:]) / len(inference_times[1:])
            if len(inference_times) > 1
            else inference_times[0],
            2,
        ),
        "observation": {
            "track_id": observation.track_id,
            "positive": observation.positive,
            "decision_score": round(observation.score, 6),
            "classifier_score": round(observation.classifier_score or 0.0, 6),
            "object_score": round(observation.object_score or 0.0, 6),
            "signal_sources": list(observation.signal_sources),
        },
        "object_detections": len(engine.object_detector.last_detections),
        "providers": {
            "classifier": engine.active_providers,
            "objects": engine.object_detector.active_providers,
        },
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
