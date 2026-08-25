#!/usr/bin/env python3
# ruff: noqa: E402, I001
"""Sweep fire/smoke thresholds and ROI policy from one inference pass."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "app" / "src"))

from ls_vision.adapters.models.fire_smoke_engine import FireSmokeEngine
from ls_vision.application.safety_replay import score_presence
from ls_vision.bootstrap.config import load_config
from run_safety_fixture_replay import _load_manifest, _select_rows


def _quantiles(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p90": None, "p95": None, "max": None}
    array = np.asarray(values, dtype=np.float32)
    return {
        "count": len(values),
        "p50": float(np.quantile(array, 0.50)),
        "p90": float(np.quantile(array, 0.90)),
        "p95": float(np.quantile(array, 0.95)),
        "max": float(array.max()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("app/tests/fixtures/safety_ground_truth.yaml"),
    )
    parser.add_argument("--config", type=Path, default=Path("app/config/dev.yaml"))
    parser.add_argument("--camera", default="camera_safety")
    parser.add_argument("--provider", default="CPUExecutionProvider")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--report", type=Path, default=Path(".tmp/safety-replay/calibration.json")
    )
    args = parser.parse_args()

    manifest, annotations = _load_manifest(args.manifest)
    config = load_config(args.config, args.camera)
    config["fire_smoke"]["providers"] = [args.provider]
    config["fire_smoke"]["require_gpu_provider"] = args.provider != "CPUExecutionProvider"
    config["fire_smoke"]["onnx_path"] = str(args.model.resolve())
    engine = FireSmokeEngine(config)
    safety_rois = dict(engine.class_rois)
    fire_thresholds = (0.20, 0.30, 0.40, 0.50)
    smoke_thresholds = (0.05, 0.10, 0.20, 0.30, 0.40)
    candidates = [
        (roi_name, fire_threshold, smoke_threshold)
        for roi_name in ("camera", "full_frame")
        for fire_threshold in fire_thresholds
        for smoke_threshold in smoke_thresholds
    ]
    rows_by_candidate: dict[tuple[str, float, float], list[tuple[set[str], set[str]]]] = {
        candidate: [] for candidate in candidates
    }
    raw_scores: dict[str, dict[str, list[float]]] = {
        label: {"positive": [], "negative": [], "assigned_positive": [], "assigned_negative": []}
        for label in ("fire", "smoke")
    }
    latencies: list[float] = []
    maximum = int((manifest.get("sampling", {}) or {}).get("max_frames_per_video", 60))

    for case in manifest["cases"]:
        selected = _select_rows(list(annotations[str(case["annotation_key"])]), maximum)
        capture = cv2.VideoCapture(str(ROOT / str(case["video"])))
        if not capture.isOpened():
            raise RuntimeError(f"unable to open fixture: {case['video']}")
        try:
            for annotation in selected:
                frame_number = int(annotation["frame_num"])
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError(f"unable to read {case['id']} frame {frame_number}")
                expected = {
                    str(item["class"])
                    for item in annotation.get("objects", [])
                    if str(item.get("class")) in {"fire", "smoke"}
                }
                image, ratio, pad_x, pad_y = engine._letterbox(frame)
                tensor = (
                    cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    .astype(np.float32)
                    .transpose(2, 0, 1)[None]
                    / 255.0
                )
                started = time.perf_counter()
                output = engine.session.run(None, {engine.input_name: tensor})[0]
                latencies.append(time.perf_counter() - started)
                matrix = np.asarray(output)[0].T
                assigned = np.argmax(matrix[:, 4:], axis=1)
                for index, label in enumerate(("fire", "smoke")):
                    presence = "positive" if label in expected else "negative"
                    raw_scores[label][presence].append(float(matrix[:, 4 + index].max()))
                    assigned_scores = matrix[assigned == index, 4 + index]
                    raw_scores[label][f"assigned_{presence}"].append(
                        float(assigned_scores.max()) if assigned_scores.size else 0.0
                    )
                for candidate in candidates:
                    roi_name, fire_threshold, smoke_threshold = candidate
                    engine.class_rois = safety_rois if roi_name == "camera" else {}
                    engine.fire_threshold = fire_threshold
                    engine.smoke_threshold = smoke_threshold
                    detections = engine._decode(output, frame, ratio, pad_x, pad_y)
                    rows_by_candidate[candidate].append(
                        (expected, {str(item.label) for item in detections})
                    )
        finally:
            capture.release()

    scored: list[dict[str, Any]] = []
    for (roi_name, fire_threshold, smoke_threshold), rows in rows_by_candidate.items():
        metrics = score_presence(rows)
        scored.append(
            {
                "roi": roi_name,
                "fire_threshold": fire_threshold,
                "smoke_threshold": smoke_threshold,
                **metrics,
            }
        )
    scored.sort(
        key=lambda item: (
            float(item["macro_f1"]),
            float(item["classes"]["smoke"]["recall"]),
            float(item["classes"]["fire"]["precision"]),
        ),
        reverse=True,
    )
    report = {
        "measurement_valid": True,
        "sample_count": len(next(iter(rows_by_candidate.values()))),
        "providers": engine.active_providers,
        "raw_scores": {
            label: {group: _quantiles(values) for group, values in groups.items()}
            for label, groups in raw_scores.items()
        },
        "inference_p50_seconds": float(np.quantile(latencies, 0.50)),
        "inference_p95_seconds": float(np.quantile(latencies, 0.95)),
        "recommended": scored[0],
        "candidates": scored,
        "note": "Frame-presence calibration only; temporal event acceptance remains separate.",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(args.report),
                "recommended": {
                    key: report["recommended"][key]
                    for key in ("roi", "fire_threshold", "smoke_threshold", "macro_f1")
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
