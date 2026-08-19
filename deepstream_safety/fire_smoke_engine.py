"""ONNX Runtime fire/smoke detector for full-frame analysis."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("deepstream.fire_smoke")


@dataclass(frozen=True)
class FireSmokeDetection:
    label: str
    score: float
    bbox: tuple[float, float, float, float]


class FireSmokeEngine:
    """Run the exported YOLO fire/smoke model on the complete camera frame."""

    def __init__(self, config: dict[str, Any]) -> None:
        runtime = config.get("fire_smoke", {}) or {}
        self.camera_id = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.enabled = bool(runtime.get("enabled", False))
        self.input_width = int(runtime.get("input_width", 640))
        self.input_height = int(runtime.get("input_height", 640))
        self.fire_threshold = float(runtime.get("fire_threshold", 0.25))
        self.smoke_threshold = float(runtime.get("smoke_threshold", 0.20))
        self.interval_seconds = max(0.2, float(runtime.get("interval_ms", 300)) / 1000.0)
        self.detection_hold_seconds = max(
            self.interval_seconds * 2.0,
            float(runtime.get("detection_hold_seconds", 1.0)),
        )
        self.nms_iou = float(runtime.get("nms_iou", 0.45))
        self.max_detections_per_label = max(1, int(runtime.get("max_detections_per_label", 1)))
        self.smoothing_alpha = min(1.0, max(0.05, float(runtime.get("bbox_smoothing_alpha", 0.35))))
        self.smoothing_clear_seconds = max(
            1.0, float(runtime.get("bbox_smoothing_clear_seconds", runtime.get("clear_seconds", 3.0)))
        )
        self.class_rois = {
            str(label): tuple(float(value) for value in roi)
            for label, roi in (runtime.get("class_rois", {}) or {}).items()
        }
        self._last_attempt = 0.0
        self._smoothed: dict[str, tuple[tuple[float, float, float, float], float]] = {}
        self._cached_detections: list[FireSmokeDetection] = []
        self._last_nonempty_at = 0.0
        self.session = None
        self.input_name = ""
        self.active_providers: list[str] = []
        self.labels = tuple(str(label) for label in runtime.get("labels", ["fire", "smoke"]))

        if not self.enabled:
            return
        model_path = Path(str(runtime.get("onnx_path", "")))
        if not model_path.is_file():
            raise FileNotFoundError(f"fire/smoke ONNX model not found: {model_path}")
        import onnxruntime as ort

        available = list(ort.get_available_providers())
        requested = [
            str(provider)
            for provider in runtime.get(
                "providers", ["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
        ]
        selected = [provider for provider in requested if provider in available]
        if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in selected:
            selected.append("CPUExecutionProvider")
        gpu_providers = {"TensorrtExecutionProvider", "CUDAExecutionProvider"}
        if bool(runtime.get("require_gpu_provider", False)) and not gpu_providers.intersection(
            selected
        ):
            raise RuntimeError(
                "GPU fire/smoke provider requested but unavailable; "
                f"requested={requested} available={available}"
            )
        self.session = ort.InferenceSession(str(model_path), providers=selected)
        self.input_name = self.session.get_inputs()[0].name
        self.active_providers = list(self.session.get_providers())
        if bool(runtime.get("require_gpu_provider", False)) and not gpu_providers.intersection(
            self.active_providers
        ):
            raise RuntimeError(
                "GPU fire/smoke provider was not active; "
                f"requested={requested} active={self.active_providers}"
            )
        LOG.info(
            "fire/smoke active: camera=%s model=%s providers=%s thresholds=(fire %.2f, smoke %.2f)",
            self.camera_id,
            model_path,
            self.active_providers,
            self.fire_threshold,
            self.smoke_threshold,
        )

    def _letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float, float, float]:
        height, width = frame.shape[:2]
        ratio = min(self.input_width / width, self.input_height / height)
        resized_width = max(1, int(round(width * ratio)))
        resized_height = max(1, int(round(height * ratio)))
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.input_height, self.input_width, 3), 114, dtype=np.uint8)
        pad_x = (self.input_width - resized_width) / 2.0
        pad_y = (self.input_height - resized_height) / 2.0
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        canvas[top : top + resized_height, left : left + resized_width] = resized
        return canvas, ratio, float(left), float(top)

    def _threshold(self, label: str) -> float:
        return self.fire_threshold if label == "fire" else self.smoke_threshold

    def _in_class_roi(self, label: str, bbox: tuple[float, float, float, float], frame: np.ndarray) -> bool:
        roi = self.class_rois.get(label)
        if roi is None or len(roi) != 4:
            return True
        frame_height, frame_width = frame.shape[:2]
        roi_left, roi_top, roi_right, roi_bottom = (
            roi[0] * frame_width,
            roi[1] * frame_height,
            roi[2] * frame_width,
            roi[3] * frame_height,
        )
        center_x = (bbox[0] + bbox[2]) / 2.0
        center_y = (bbox[1] + bbox[3]) / 2.0
        return roi_left <= center_x <= roi_right and roi_top <= center_y <= roi_bottom

    def _decode(self, output: np.ndarray, frame: np.ndarray, ratio: float, pad_x: float, pad_y: float) -> list[FireSmokeDetection]:
        predictions = np.asarray(output)
        if predictions.ndim == 3:
            predictions = predictions[0]
        if predictions.ndim != 2:
            raise RuntimeError(f"fire/smoke model returned invalid output shape: {predictions.shape}")
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.T
        if predictions.shape[1] < 5:
            raise RuntimeError(f"fire/smoke model returned invalid prediction width: {predictions.shape}")

        frame_height, frame_width = frame.shape[:2]
        by_label: dict[str, list[tuple[list[float], float]]] = {label: [] for label in self.labels}
        for row in predictions:
            class_scores = row[4:]
            class_index = int(np.argmax(class_scores))
            if class_index >= len(self.labels):
                continue
            label = self.labels[class_index]
            score = float(class_scores[class_index])
            if score < self._threshold(label):
                continue
            center_x, center_y, box_width, box_height = [float(value) for value in row[:4]]
            left = max(0.0, (center_x - box_width / 2.0 - pad_x) / ratio)
            top = max(0.0, (center_y - box_height / 2.0 - pad_y) / ratio)
            right = min(float(frame_width), (center_x + box_width / 2.0 - pad_x) / ratio)
            bottom = min(float(frame_height), (center_y + box_height / 2.0 - pad_y) / ratio)
            bbox = (left, top, right, bottom)
            if right > left and bottom > top and self._in_class_roi(label, bbox, frame):
                by_label[label].append(([left, top, right - left, bottom - top], score))

        detections: list[FireSmokeDetection] = []
        for label, candidates in by_label.items():
            if not candidates:
                continue
            indices = cv2.dnn.NMSBoxes(
                [box for box, _ in candidates],
                [score for _, score in candidates],
                self._threshold(label),
                self.nms_iou,
            )
            selected: list[FireSmokeDetection] = []
            for raw_index in indices:
                index = int(raw_index[0] if isinstance(raw_index, list | tuple | np.ndarray) else raw_index)
                box, score = candidates[index]
                selected.append(
                    FireSmokeDetection(
                        label=label,
                        score=score,
                        bbox=(box[0], box[1], box[0] + box[2], box[1] + box[3]),
                    )
                )
            detections.extend(selected[: self.max_detections_per_label])
        return detections

    def process(self, frame: np.ndarray) -> list[FireSmokeDetection]:
        if not self.enabled or self.session is None or frame.size == 0:
            return []
        now = time.monotonic()
        if now - self._last_attempt < self.interval_seconds:
            return list(self._cached_detections)
        self._last_attempt = now
        image, ratio, pad_x, pad_y = self._letterbox(frame)
        tensor = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        output = self.session.run(None, {self.input_name: tensor})[0]
        detections = self._decode(output, frame, ratio, pad_x, pad_y)
        now = time.monotonic()
        current_labels = {detection.label for detection in detections}
        for label in tuple(self._smoothed):
            if label not in current_labels and now - self._smoothed[label][1] > self.smoothing_clear_seconds:
                self._smoothed.pop(label, None)
        smoothed: list[FireSmokeDetection] = []
        for detection in detections:
            previous = self._smoothed.get(detection.label)
            if previous is not None and now - previous[1] <= self.smoothing_clear_seconds:
                old_bbox = np.asarray(previous[0], dtype=np.float32)
                current_bbox = np.asarray(detection.bbox, dtype=np.float32)
                bbox = tuple(
                    float(value)
                    for value in (old_bbox * (1.0 - self.smoothing_alpha) + current_bbox * self.smoothing_alpha)
                )
            else:
                bbox = detection.bbox
            self._smoothed[detection.label] = (bbox, now)
            smoothed.append(
                FireSmokeDetection(label=detection.label, score=detection.score, bbox=bbox)
            )
        if smoothed:
            self._cached_detections = smoothed
            self._last_nonempty_at = now
        elif now - self._last_nonempty_at > self.detection_hold_seconds:
            self._cached_detections = []
        return list(self._cached_detections)
