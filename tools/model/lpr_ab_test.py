"""Deterministic, model-only A/B measurements for the native camera-1 LPR model.

This deliberately does not change Frigate or the active model.  It measures both
ONNX files on the same sampled frames and records score-range and duplicate-box
signals that are otherwise easy to miss in an event-only review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIDEO = ROOT / "mock_videos/car-number-plate-video/cam-in/Traffic Control CCTV.mp4"
DEFAULT_MODELS = [
    ROOT / "models/roboflow-logistics-yolov8/best.onnx",
    ROOT / "models/roboflow-logistics-yolov8/best-frigate.onnx",
]
PLATE_CLASS = 9
CAR_CLASS = 1


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iou(left: np.ndarray, right: np.ndarray) -> float:
    x1 = max(float(left[0]), float(right[0]))
    y1 = max(float(left[1]), float(right[1]))
    x2 = min(float(left[2]), float(right[2]))
    y2 = min(float(left[3]), float(right[3]))
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_left = max(0.0, float(left[2] - left[0])) * max(0.0, float(left[3] - left[1]))
    area_right = max(0.0, float(right[2] - right[0])) * max(0.0, float(right[3] - right[1]))
    union = area_left + area_right - intersection
    return intersection / union if union else 0.0


def prepare(frame: np.ndarray) -> np.ndarray:
    image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = cv2.resize(image, (640, 640), interpolation=cv2.INTER_LINEAR)
    return (image.transpose(2, 0, 1)[None].astype(np.float32) / 255.0)


def detections(raw: np.ndarray, width: int, height: int, threshold: float) -> dict[str, list[dict]]:
    # YOLOv8 exported output is [x, y, w, h, class_scores...] with no NMS.
    output = raw[0].T
    classes = np.argmax(output[:, 4:], axis=1)
    scores = output[np.arange(output.shape[0]), 4 + classes]
    result: dict[str, list[dict]] = {"car": [], "license_plate": []}
    for row, cls, score in zip(output, classes, scores):
        if cls not in (CAR_CLASS, PLATE_CLASS) or float(score) < threshold:
            continue
        x, y, w, h = row[:4]
        box = np.array(
            [
                (x - w / 2) * width / 640,
                (y - h / 2) * height / 640,
                (x + w / 2) * width / 640,
                (y + h / 2) * height / 640,
            ],
            dtype=np.float32,
        )
        result["car" if cls == CAR_CLASS else "license_plate"].append(
            {"box": box.tolist(), "score": float(score)}
        )
    return result


def summarize(model: Path, video: Path, samples: int, threshold: float) -> dict:
    capture = cv2.VideoCapture(str(video))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    indices = np.linspace(0, max(0, frame_count - 1), samples, dtype=int)
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    latencies: list[float] = []
    frame_results: list[dict] = []
    for index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = capture.read()
        if not ok:
            continue
        started = time.perf_counter()
        raw = session.run(None, {input_name: prepare(frame)})[0]
        latencies.append((time.perf_counter() - started) * 1000)
        found = detections(raw, width, height, threshold)
        plates = found["license_plate"]
        duplicate_pairs = sum(
            iou(np.asarray(a["box"]), np.asarray(b["box"])) >= 0.8
            for position, a in enumerate(plates)
            for b in plates[position + 1 :]
        )
        frame_results.append(
            {
                "frame": int(index),
                "car_count": len(found["car"]),
                "plate_count": len(plates),
                "plate_scores": [item["score"] for item in plates],
                "duplicate_pairs_iou_ge_0_8": duplicate_pairs,
            }
        )
    capture.release()
    plate_scores = [score for item in frame_results for score in item["plate_scores"]]
    return {
        "model": model.name,
        "path": str(model),
        "sha256": sha256(model),
        "threshold": threshold,
        "frames_sampled": len(frame_results),
        "video": {"path": str(video), "frame_count": frame_count, "width": width, "height": height},
        "frames_with_car": sum(item["car_count"] > 0 for item in frame_results),
        "frames_with_plate": sum(item["plate_count"] > 0 for item in frame_results),
        "plate_boxes": sum(item["plate_count"] for item in frame_results),
        "duplicate_pairs": sum(item["duplicate_pairs_iou_ge_0_8"] for item in frame_results),
        "plate_scores_over_1": sum(score > 1.0 for score in plate_scores),
        "plate_score_max": max(plate_scores, default=None),
        "inference_ms": {
            "mean": float(np.mean(latencies)) if latencies else None,
            "p95": float(np.percentile(latencies, 95)) if latencies else None,
        },
        "frames": frame_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--model", type=Path, action="append")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp/model-review/lpr-ab.json")
    args = parser.parse_args()
    models = args.model or DEFAULT_MODELS
    report = {
        "kind": "native-camera-1-lpr-model-ab",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": [summarize(path.resolve(), args.video.resolve(), args.samples, args.threshold) for path in models],
        "runtime_note": "Model-only CPU measurement; Frigate OCR, CPU/GPU container usage, and event recall require a separate runtime replay.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "models": report["models"]}, indent=2))


if __name__ == "__main__":
    main()
