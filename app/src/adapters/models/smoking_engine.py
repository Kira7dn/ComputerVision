"""Stateless smoking-behavior scoring for frame-aligned person ROIs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from adapters.models.smoking_object_detector import SmokingObjectDetector
from domain.smoking_events import SmokingInferenceBatch, SmokingObservation

LOG = logging.getLogger("deepstream.smoking_behavior")


class SmokingBehaviorEngine:
    """Score every valid person crop; temporal state belongs to the domain."""

    def __init__(self, config: dict[str, Any]) -> None:
        runtime = config.get("smoking_behavior", {}) or {}
        self.camera_id = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.enabled = bool(runtime.get("enabled", False))
        self.input_width = int(runtime.get("input_width", 224))
        self.input_height = int(runtime.get("input_height", 224))
        self.threshold = float(runtime.get("smoking_threshold", 0.60))
        self.padding_ratio = float(runtime.get("padding_ratio", 0.20))
        self.interval_seconds = max(
            0.3, min(0.5, float(runtime.get("interval_ms", 400)) / 1000.0)
        )
        self.last_person_count = 0
        self.last_scores: dict[int, float] = {}
        self.last_roi_bboxes: dict[int, tuple[int, int, int, int]] = {}
        self.last_invalid_crop_track_ids: list[int] = []
        self.last_object_scores: dict[int, float] = {}
        self.last_signal_sources: dict[int, list[str]] = {}
        self.session = None
        self.input_name = ""
        self.active_providers: list[str] = []
        self.object_detector = SmokingObjectDetector(config)

        if not self.enabled:
            return
        model_path = Path(str(runtime.get("onnx_path", "")))
        if not model_path.is_file():
            raise FileNotFoundError(f"smoking behavior model not found: {model_path}")
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
                "GPU smoking behavior provider requested but unavailable; "
                f"requested={requested} available={available}"
            )
        self.session = ort.InferenceSession(str(model_path), providers=selected)
        self.input_name = self.session.get_inputs()[0].name
        self.active_providers = list(self.session.get_providers())
        if bool(runtime.get("require_gpu_provider", False)) and not gpu_providers.intersection(
            self.active_providers
        ):
            raise RuntimeError(
                "GPU smoking behavior provider was not active; "
                f"requested={requested} active={self.active_providers}"
            )
        if bool(runtime.get("warmup", True)):
            self.session.run(
                None,
                {
                    self.input_name: np.zeros(
                        (1, 3, self.input_height, self.input_width), dtype=np.float32
                    )
                },
            )
        LOG.info(
            "smoking scorer active: camera=%s model=%s providers=%s threshold=%.2f interval=%.0fms",
            self.camera_id,
            model_path,
            self.active_providers,
            self.threshold,
            self.interval_seconds * 1000,
        )

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            inverse = np.exp(-value)
            return float(1.0 / (1.0 + inverse))
        exponent = np.exp(value)
        return float(exponent / (1.0 + exponent))

    def _score(self, crop: np.ndarray) -> float:
        resized = cv2.resize(
            crop, (self.input_width, self.input_height), interpolation=cv2.INTER_AREA
        )
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = ((rgb - 0.5) / 0.5).transpose(2, 0, 1)[None, ...]
        logits = np.asarray(
            self.session.run(None, {self.input_name: tensor})[0], dtype=np.float32
        )
        if logits.ndim == 2:
            logits = logits[0]
        if logits.size < 2:
            raise RuntimeError(
                f"smoking behavior model returned invalid logits: {logits.shape}"
            )
        return self._sigmoid(float(logits[1]))

    def process(
        self,
        frame: np.ndarray,
        persons: list[tuple[int, float, float, float, float]],
        frame_number: int,
    ) -> SmokingInferenceBatch:
        del frame_number
        observed_track_ids = tuple(sorted({int(person[0]) for person in persons}))
        if not self.enabled or self.session is None or frame.size == 0:
            return SmokingInferenceBatch((), observed_track_ids)
        frame_height, frame_width = frame.shape[:2]
        observations: list[SmokingObservation] = []
        invalid: list[int] = []
        object_detections = self.object_detector.process(frame)
        person_bboxes = {
            int(track_id): (
                float(raw_left),
                float(raw_top),
                float(raw_right),
                float(raw_bottom),
            )
            for track_id, raw_left, raw_top, raw_right, raw_bottom in persons
        }
        assigned_objects: dict[int, list[Any]] = {track_id: [] for track_id in person_bboxes}
        for detection in object_detections:
            candidates = [
                (
                    self.object_detector.person_match_score(detection, person_bbox),
                    track_id,
                )
                for track_id, person_bbox in person_bboxes.items()
            ]
            score, track_id = max(candidates, default=(0.0, -1), key=lambda item: (item[0], -item[1]))
            if score > 0.0:
                assigned_objects[track_id].append(detection)
        self.last_person_count = len(persons)
        self.last_scores = {}
        self.last_roi_bboxes = {}
        self.last_object_scores = {}
        self.last_signal_sources = {}
        for track_id, raw_left, raw_top, raw_right, raw_bottom in persons:
            width = max(1.0, raw_right - raw_left)
            height = max(1.0, raw_bottom - raw_top)
            pad_x = width * self.padding_ratio
            pad_y = height * self.padding_ratio
            left = max(0, int(raw_left - pad_x))
            top = max(0, int(raw_top - pad_y))
            right = min(frame_width, int(raw_right + pad_x))
            bottom = min(frame_height, int(raw_bottom + pad_y))
            crop = frame[top:bottom, left:right]
            if crop.size == 0 or right <= left or bottom <= top:
                invalid.append(int(track_id))
                continue
            score = float(self._score(crop))
            person_bbox = person_bboxes[int(track_id)]
            matched = assigned_objects[int(track_id)]
            object_score = max((detection.score for detection in matched), default=0.0)
            sources = tuple(
                sorted({f"tbox:{detection.source}:{detection.label}" for detection in matched})
            )
            classifier_positive = score >= self.threshold
            object_positive = bool(matched)
            decision_score = max(
                score,
                self.threshold if object_positive else 0.0,
            )
            self.last_scores[int(track_id)] = score
            self.last_roi_bboxes[int(track_id)] = (left, top, right, bottom)
            self.last_object_scores[int(track_id)] = object_score
            self.last_signal_sources[int(track_id)] = list(sources)
            observations.append(
                SmokingObservation(
                    track_id=int(track_id),
                    score=decision_score,
                    person_bbox=person_bbox,
                    model_roi_bbox=(
                        float(left), float(top), float(right), float(bottom)
                    ),
                    positive=classifier_positive or object_positive,
                    classifier_score=score,
                    object_score=object_score,
                    signal_sources=("person_classifier",) + sources
                    if classifier_positive
                    else sources,
                )
            )
        self.last_invalid_crop_track_ids = sorted(invalid)
        return SmokingInferenceBatch(
            tuple(observations), observed_track_ids, tuple(sorted(invalid))
        )
