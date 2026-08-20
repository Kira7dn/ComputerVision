"""Run smoking-behavior classification on tracked person ROIs."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("deepstream.smoking_behavior")


@dataclass(frozen=True)
class SmokingDetection:
    track_id: int
    person_bbox: tuple[float, float, float, float]
    model_roi_bbox: tuple[float, float, float, float]
    score: float

    @property
    def box(self) -> np.ndarray:
        """Backward-compatible person bbox plus score for legacy callers."""
        return np.array([*self.person_bbox, self.score], dtype=np.float32)


class SmokingBehaviorEngine:
    """Classify each person ROI and expose temporally confirmed smoking boxes."""

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
        self.confirmation_hits = max(1, int(runtime.get("confirmation_hits", 2)))
        self.confirmation_window = max(
            self.confirmation_hits, int(runtime.get("confirmation_window", 4))
        )
        self.clear_hits = max(1, int(runtime.get("clear_hits", 4)))
        self._last_attempt: dict[int, float] = {}
        self._history: dict[int, list[float]] = {}
        self._cached: dict[int, tuple[float, tuple[int, int, int, int]]] = {}
        self.track_grace_frames = max(1, int(runtime.get("track_grace_frames", 30)))
        self.bbox_smoothing_alpha = min(
            1.0, max(0.05, float(runtime.get("bbox_smoothing_alpha", 0.35)))
        )
        self._missing_frames: dict[int, int] = {}
        self._smoothed_person_bbox: dict[int, tuple[float, float, float, float]] = {}
        self._active_tracks: set[int] = set()
        self._below_threshold: dict[int, int] = {}
        self.last_person_count = 0
        self.last_scores: dict[int, float] = {}
        self.last_histories: dict[int, list[float]] = {}
        self.last_roi_bboxes: dict[int, tuple[int, int, int, int]] = {}
        self.last_confirmed_tracks: list[int] = []
        self.session = None
        self.active_providers: list[str] = []

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
        self.active_providers = list(self.session.get_providers())
        if bool(runtime.get("require_gpu_provider", False)) and not gpu_providers.intersection(
            self.active_providers
        ):
            raise RuntimeError(
                "GPU smoking behavior provider was not active; "
                f"requested={requested} active={self.active_providers}"
            )
        LOG.info(
            "smoking behavior active: camera=%s model=%s providers=%s threshold=%.2f interval=%.0fms",
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
        # ViTImageProcessor for this model uses mean/std=(0.5, 0.5, 0.5).
        tensor = ((rgb - 0.5) / 0.5).transpose(2, 0, 1)[None, ...]
        input_name = self.session.get_inputs()[0].name
        logits = np.asarray(self.session.run(None, {input_name: tensor})[0], dtype=np.float32)
        if logits.ndim == 2:
            logits = logits[0]
        if logits.size < 2:
            raise RuntimeError(f"smoking behavior model returned invalid logits: {logits.shape}")
        # The Transformers image-classification pipeline uses independent sigmoid
        # scores for this binary checkpoint, so keep the benchmark score identical.
        return self._sigmoid(float(logits[1]))

    def process(
        self,
        frame: np.ndarray,
        persons: list[tuple[int, float, float, float, float]],
        frame_number: int,
    ) -> list[SmokingDetection]:
        if not self.enabled or self.session is None or frame.size == 0:
            return []
        frame_height, frame_width = frame.shape[:2]
        now = time.monotonic()
        result: list[SmokingDetection] = []
        self.last_person_count = len(persons)
        seen: set[int] = set()
        for track_id, raw_left, raw_top, raw_right, raw_bottom in persons:
            seen.add(track_id)
            self._missing_frames[track_id] = 0
            width = max(1.0, raw_right - raw_left)
            height = max(1.0, raw_bottom - raw_top)
            pad_x = width * self.padding_ratio
            pad_y = height * self.padding_ratio
            left = max(0, int(raw_left - pad_x))
            top = max(0, int(raw_top - pad_y))
            right = min(frame_width, int(raw_right + pad_x))
            bottom = min(frame_height, int(raw_bottom + pad_y))
            crop = frame[top:bottom, left:right]
            if crop.size == 0:
                continue
            due = now - self._last_attempt.get(track_id, 0.0) >= self.interval_seconds
            if due:
                self._last_attempt[track_id] = now
                score = self._score(crop)
                self.last_scores[track_id] = float(score)
                history = self._history.setdefault(track_id, [])
                history.append(score)
                del history[:-self.confirmation_window]
                self._cached[track_id] = (score, (left, top, right, bottom))
                self.last_histories[track_id] = list(history)
                self.last_roi_bboxes[track_id] = (left, top, right, bottom)
            cached = self._cached.get(track_id)
            if cached is None:
                continue
            score, roi_bbox = cached
            history = self._history.get(track_id, [])
            confirmed = sum(value >= self.threshold for value in history) >= self.confirmation_hits
            if confirmed:
                self._active_tracks.add(track_id)
                self._below_threshold[track_id] = 0
            elif due and track_id in self._active_tracks:
                self._below_threshold[track_id] = self._below_threshold.get(track_id, 0) + 1
                if self._below_threshold[track_id] >= self.clear_hits:
                    self._active_tracks.discard(track_id)
            if track_id not in self._active_tracks:
                continue
            current_person_bbox = np.asarray(
                [raw_left, raw_top, raw_right, raw_bottom], dtype=np.float32
            )
            previous_person_bbox = self._smoothed_person_bbox.get(track_id)
            if previous_person_bbox is not None:
                current_person_bbox = (
                    np.asarray(previous_person_bbox, dtype=np.float32)
                    * (1.0 - self.bbox_smoothing_alpha)
                    + current_person_bbox * self.bbox_smoothing_alpha
                )
            person_bbox = tuple(float(value) for value in current_person_bbox)
            self._smoothed_person_bbox[track_id] = person_bbox
            result.append(
                SmokingDetection(
                    track_id=track_id,
                    person_bbox=person_bbox,
                    model_roi_bbox=tuple(float(value) for value in roi_bbox),
                    score=score,
                )
            )
        for track_id in set(self._history) - seen:
            self._missing_frames[track_id] = self._missing_frames.get(track_id, 0) + 1
            if self._missing_frames[track_id] <= self.track_grace_frames:
                continue
            self._history.pop(track_id, None)
            self._cached.pop(track_id, None)
            self._last_attempt.pop(track_id, None)
            self._smoothed_person_bbox.pop(track_id, None)
            self._active_tracks.discard(track_id)
            self._below_threshold.pop(track_id, None)
            self._missing_frames.pop(track_id, None)
            self.last_scores.pop(track_id, None)
            self.last_histories.pop(track_id, None)
            self.last_roi_bboxes.pop(track_id, None)
        self.last_confirmed_tracks = sorted(self._active_tracks)
        return result
