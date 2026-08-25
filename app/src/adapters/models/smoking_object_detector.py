"""ONNX adapter for the T-Box cigarette and smoking object detectors."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("deepstream.smoking_object_detector")

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class SmokingObjectDetection:
    source: str
    label: str
    score: float
    bbox: BBox


@dataclass
class _Model:
    source: str
    labels: tuple[str, ...]
    positive_labels: frozenset[str]
    session: Any
    input_name: str


class SmokingObjectDetector:
    """Run configured T-Box ONNX detectors once per full frame.

    The adapter is shared by the legacy smoking function and the DMS function.
    The latter configures every model class as a positive class so the DMS
    engine can apply its own class mapping and alert policy.
    """

    def __init__(self, config: dict[str, Any], section: str = "smoking_behavior") -> None:
        smoking = config.get(section, {}) or {}
        runtime = smoking.get("object_detection", {}) or {}
        self.section = section
        self.enabled = bool(runtime.get("enabled", False))
        self.input_width = int(runtime.get("input_width", 640))
        self.input_height = int(runtime.get("input_height", 640))
        self.confidence = float(runtime.get("confidence", 0.35))
        self.nms_iou = float(runtime.get("nms_iou", 0.50))
        self.person_match_iou = float(runtime.get("person_match_iou", 0.10))
        self.models: list[_Model] = []
        self.active_providers: dict[str, list[str]] = {}
        self.last_detections: list[SmokingObjectDetection] = []
        self.last_raw_scores: dict[str, float] = {}
        if not self.enabled:
            return

        import onnxruntime as ort

        available = list(ort.get_available_providers())
        requested = [
            str(provider)
            for provider in smoking.get(
                "providers", ["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
        ]
        selected = [provider for provider in requested if provider in available]
        if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in selected:
            selected.append("CPUExecutionProvider")
        gpu_providers = {"TensorrtExecutionProvider", "CUDAExecutionProvider"}
        if bool(smoking.get("require_gpu_provider", False)) and not gpu_providers.intersection(
            selected
        ):
            raise RuntimeError(
                "GPU smoking object provider requested but unavailable; "
                f"requested={requested} available={available}"
            )

        model_configs = runtime.get("models", {}) or {}
        if not isinstance(model_configs, dict) or not model_configs:
            raise ValueError("smoking object_detection.models must be a non-empty mapping")
        for source, model_config in sorted(model_configs.items()):
            model_path = Path(str((model_config or {}).get("onnx_path", "")))
            if not model_path.is_file():
                raise FileNotFoundError(f"smoking object model not found: {model_path}")
            labels = tuple(str(label) for label in (model_config or {}).get("labels", ()))
            positive_labels = frozenset(
                str(label) for label in (model_config or {}).get("positive_labels", ())
            )
            if not labels or not positive_labels or not positive_labels.issubset(labels):
                raise ValueError(
                    f"smoking object model {source} has invalid labels/positive_labels"
                )
            session = ort.InferenceSession(str(model_path), providers=selected)
            input_name = session.get_inputs()[0].name
            active = list(session.get_providers())
            if bool(smoking.get("require_gpu_provider", False)) and not gpu_providers.intersection(
                active
            ):
                raise RuntimeError(
                    f"GPU provider was not active for smoking object model {source}: {active}"
                )
            if bool(runtime.get("warmup", True)):
                session.run(
                    None,
                    {
                        input_name: np.zeros(
                            (1, 3, self.input_height, self.input_width), dtype=np.float32
                        )
                    },
                )
            self.models.append(
                _Model(str(source), labels, positive_labels, session, input_name)
            )
            self.active_providers[str(source)] = active
        LOG.info(
            "T-Box object detectors active: section=%s models=%s threshold=%.2f providers=%s",
            self.section,
            [model.source for model in self.models],
            self.confidence,
            self.active_providers,
        )

    def _letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float, float, float]:
        height, width = frame.shape[:2]
        ratio = min(self.input_width / width, self.input_height / height)
        resized_width = max(1, int(round(width * ratio)))
        resized_height = max(1, int(round(height * ratio)))
        resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.input_height, self.input_width, 3), 114, dtype=np.uint8)
        left = int(round((self.input_width - resized_width) / 2.0 - 0.1))
        top = int(round((self.input_height - resized_height) / 2.0 - 0.1))
        canvas[top : top + resized_height, left : left + resized_width] = resized
        return canvas, ratio, float(left), float(top)

    def _decode(
        self,
        model: _Model,
        output: np.ndarray,
        frame: np.ndarray,
        ratio: float,
        pad_x: float,
        pad_y: float,
    ) -> list[SmokingObjectDetection]:
        predictions = np.asarray(output)
        if predictions.ndim == 3:
            predictions = predictions[0]
        if predictions.ndim != 2:
            raise RuntimeError(
                f"smoking object model {model.source} returned invalid shape {predictions.shape}"
            )
        expected_width = 4 + len(model.labels)
        if predictions.shape[0] == expected_width:
            predictions = predictions.T
        if predictions.shape[1] != expected_width:
            raise RuntimeError(
                f"smoking object model {model.source} returned invalid width {predictions.shape}"
            )

        frame_height, frame_width = frame.shape[:2]
        candidates: list[tuple[list[float], float, str]] = []
        for row in predictions:
            scores = row[4:]
            class_index = int(np.argmax(scores))
            if class_index >= len(model.labels):
                continue
            label = model.labels[class_index]
            score = float(scores[class_index])
            self.last_raw_scores[f"{model.source}:{label}"] = max(
                self.last_raw_scores.get(f"{model.source}:{label}", 0.0), score
            )
            if label not in model.positive_labels or score < self.confidence:
                continue
            center_x, center_y, box_width, box_height = [float(value) for value in row[:4]]
            left = max(0.0, (center_x - box_width / 2.0 - pad_x) / ratio)
            top = max(0.0, (center_y - box_height / 2.0 - pad_y) / ratio)
            right = min(float(frame_width), (center_x + box_width / 2.0 - pad_x) / ratio)
            bottom = min(float(frame_height), (center_y + box_height / 2.0 - pad_y) / ratio)
            if right > left and bottom > top:
                candidates.append(([left, top, right - left, bottom - top], score, label))

        detections: list[SmokingObjectDetection] = []
        for label in sorted(model.positive_labels):
            selected = [candidate for candidate in candidates if candidate[2] == label]
            if not selected:
                continue
            indices = cv2.dnn.NMSBoxes(
                [item[0] for item in selected],
                [item[1] for item in selected],
                self.confidence,
                self.nms_iou,
            )
            for raw_index in indices:
                index = int(
                    raw_index[0]
                    if isinstance(raw_index, list | tuple | np.ndarray)
                    else raw_index
                )
                box, score, _ = selected[index]
                detections.append(
                    SmokingObjectDetection(
                        model.source,
                        label,
                        score,
                        (box[0], box[1], box[0] + box[2], box[1] + box[3]),
                    )
                )
        return detections

    def process(self, frame: np.ndarray) -> list[SmokingObjectDetection]:
        self.last_detections = []
        self.last_raw_scores = {}
        if not self.enabled or not self.models or frame.size == 0:
            return []
        letterboxed, ratio, pad_x, pad_y = self._letterbox(frame)
        rgb = cv2.cvtColor(letterboxed, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = rgb.transpose(2, 0, 1)[None, ...]
        for model in self.models:
            output = model.session.run(None, {model.input_name: tensor})[0]
            self.last_detections.extend(
                self._decode(model, output, frame, ratio, pad_x, pad_y)
            )
        return list(self.last_detections)

    @staticmethod
    def _iou(first: BBox, second: BBox) -> float:
        left = max(first[0], second[0])
        top = max(first[1], second[1])
        right = min(first[2], second[2])
        bottom = min(first[3], second[3])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
        second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
        union = first_area + second_area - intersection
        return intersection / union if union > 0.0 else 0.0

    def matches_person(self, detection: SmokingObjectDetection, person_bbox: BBox) -> bool:
        return self.person_match_score(detection, person_bbox) > 0.0

    def person_match_score(
        self, detection: SmokingObjectDetection, person_bbox: BBox
    ) -> float:
        center_x = (detection.bbox[0] + detection.bbox[2]) / 2.0
        center_y = (detection.bbox[1] + detection.bbox[3]) / 2.0
        center_inside = (
            person_bbox[0] <= center_x <= person_bbox[2]
            and person_bbox[1] <= center_y <= person_bbox[3]
        )
        overlap = self._iou(detection.bbox, person_bbox)
        if not center_inside and overlap < self.person_match_iou:
            return 0.0
        return (1.0 if center_inside else 0.0) + overlap
