"""DMS behavior adapter for the DeepStream camera worker.

The reference implementation in LeOS owns the DMS policy: two YOLO models,
MediaPipe face metrics, canonical alert names and a small hysteresis smoother.
This module keeps that policy intact while receiving frames from the
DeepStream probe and returning frame-aligned results to the worker.
"""

from __future__ import annotations

import logging
import math
import sys
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from adapters.models.smoking_object_detector import (
    BBox,
    SmokingObjectDetection,
    SmokingObjectDetector,
)
from domain.smoking_events import SmokingInferenceBatch, SmokingObservation

LOG = logging.getLogger("deepstream.dms")

CHAITANYA_CLASSES = ("Cigarette", "Drinking", "Eating", "Phone", "Seatbelt")
SOHAM_CLASSES = (
    "Distracted",
    "Drinking",
    "Drowsy",
    "Eating",
    "PhoneUse",
    "SafeDriving",
    "Seatbelt",
    "Smoking",
)
ALERT_CLASSES = frozenset(
    {
        "Smoking",
        "Drinking",
        "Eating",
        "Phone Usage",
        "Distracted",
        "Drowsy",
        "Yawning",
        "Eyes Closed",
        "Head Away",
        "No Seatbelt",
    }
)
LABEL_MAP = {
    "Cigarette": "Smoking",
    "Smoking": "Smoking",
    "Drinking": "Drinking",
    "Eating": "Eating",
    "Phone": "Phone Usage",
    "PhoneUse": "Phone Usage",
    "Seatbelt": "Seatbelt",
    "Distracted": "Distracted",
    "Drowsy": "Drowsy",
    "SafeDriving": "Safe Driving",
}
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH = (61, 13, 291, 14)


@dataclass(frozen=True)
class DmsDetection:
    source: str
    original_class: str
    label: str
    score: float
    bbox: BBox
    person_track_id: int | None = None


@dataclass(frozen=True)
class DmsInferenceResult:
    smoking: SmokingInferenceBatch
    detections: tuple[DmsDetection, ...]
    alerts: tuple[str, ...]
    status: str
    metrics: dict[str, Any]
    message: str | None = None


class AlertSmoother:
    """The same 3-hit-on / 2-miss-off alert hysteresis as dms.py."""

    def __init__(self, on_frames: int = 3, off_frames: int = 2) -> None:
        self.on_frames = max(1, int(on_frames))
        self.off_frames = max(1, int(off_frames))
        self.counts: dict[str, int] = {}
        self.active: dict[str, bool] = {}

    def update(self, raw_alerts: Iterable[str]) -> list[str]:
        raw = {str(item) for item in raw_alerts if str(item) in ALERT_CLASSES}
        for alert in set(self.counts) | set(self.active) | raw:
            if alert in raw:
                self.counts[alert] = min(self.on_frames, self.counts.get(alert, 0) + 1)
            else:
                self.counts[alert] = max(-self.off_frames, self.counts.get(alert, 0) - 1)
            if self.counts[alert] >= self.on_frames:
                self.active[alert] = True
            elif self.counts[alert] <= -self.off_frames:
                self.active[alert] = False
        return sorted(alert for alert, is_active in self.active.items() if is_active)


class NeutralPoseCalibrator:
    """Estimate the camera-specific straight-ahead yaw/pitch zero point."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", True))
        self.minimum_samples = max(3, int(config.get("minimum_samples", 15)))
        self.window_size = max(
            self.minimum_samples,
            int(config.get("window_size", 30)),
        )
        self.max_yaw_std = max(
            0.1,
            float(config.get("max_yaw_std_deg", 8.0)),
        )
        self.max_pitch_std = max(
            0.1,
            float(config.get("max_pitch_std_deg", 5.0)),
        )
        self.neutral_update_alpha = max(
            0.0,
            min(1.0, float(config.get("neutral_update_alpha", 0.01))),
        )
        self.neutral_update_max_delta = max(
            0.1,
            float(config.get("neutral_update_max_delta_deg", 5.0)),
        )
        self.samples: deque[tuple[float, float]] = deque(maxlen=self.window_size)
        self.neutral_yaw: float | None = None
        self.neutral_pitch: float | None = None

    @property
    def calibrated(self) -> bool:
        return not self.enabled or (
            self.neutral_yaw is not None and self.neutral_pitch is not None
        )

    def update(self, yaw: float, pitch: float) -> dict[str, Any]:
        if not self.enabled:
            return {
                "pose_calibrated": True,
                "pose_calibration_samples": 0,
                "neutral_yaw_deg": 0.0,
                "neutral_pitch_deg": 0.0,
                "yaw_deg": round(yaw, 2),
                "pitch_deg": round(pitch, 2),
            }

        if self.neutral_yaw is None or self.neutral_pitch is None:
            self.samples.append((yaw, pitch))
            if len(self.samples) >= self.minimum_samples:
                values = np.asarray(self.samples, dtype=np.float32)
                if (
                    float(np.std(values[:, 0])) <= self.max_yaw_std
                    and float(np.std(values[:, 1])) <= self.max_pitch_std
                ):
                    self.neutral_yaw = float(np.median(values[:, 0]))
                    self.neutral_pitch = float(np.median(values[:, 1]))

        calibrated = self.neutral_yaw is not None and self.neutral_pitch is not None
        if not calibrated:
            return {
                "pose_calibrated": False,
                "pose_calibration_samples": len(self.samples),
                "neutral_yaw_deg": None,
                "neutral_pitch_deg": None,
                "yaw_deg": None,
                "pitch_deg": None,
            }

        yaw_delta = yaw - float(self.neutral_yaw)
        pitch_delta = pitch - float(self.neutral_pitch)
        if (
            abs(yaw_delta) <= self.neutral_update_max_delta
            and abs(pitch_delta) <= self.neutral_update_max_delta
            and self.neutral_update_alpha > 0.0
        ):
            alpha = self.neutral_update_alpha
            self.neutral_yaw = (1.0 - alpha) * float(self.neutral_yaw) + alpha * yaw
            self.neutral_pitch = (
                (1.0 - alpha) * float(self.neutral_pitch) + alpha * pitch
            )
            yaw_delta = yaw - float(self.neutral_yaw)
            pitch_delta = pitch - float(self.neutral_pitch)
        return {
            "pose_calibrated": True,
            "pose_calibration_samples": len(self.samples),
            "neutral_yaw_deg": round(float(self.neutral_yaw), 2),
            "neutral_pitch_deg": round(float(self.neutral_pitch), 2),
            "yaw_deg": round(yaw_delta, 2),
            "pitch_deg": round(pitch_delta, 2),
        }


def _distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator > 1e-9 else 0.0


def compute_face_metrics(
    landmarks: Any,
    *,
    ear_threshold: float = 0.20,
    mar_threshold: float = 0.65,
    yaw_threshold_deg: float = 16.0,
    pitch_threshold_deg: float = 14.0,
) -> dict[str, Any]:
    """Compute the reference EAR/MAR/head pose metrics from normalized points."""

    points = [(float(point.x), float(point.y)) for point in landmarks]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    face_width = max_x - min_x
    face_height = max_y - min_y

    def eye_ratio(indices: tuple[int, ...]) -> float:
        left, top_a, top_b, right, bottom_a, bottom_b = (points[index] for index in indices)
        return _ratio(_distance(top_a, bottom_a) + _distance(top_b, bottom_b), 2.0 * _distance(left, right))

    left_eye = eye_ratio(LEFT_EYE)
    right_eye = eye_ratio(RIGHT_EYE)
    ear = (left_eye + right_eye) / 2.0
    mouth_left, mouth_top, mouth_right, mouth_bottom = (points[index] for index in MOUTH)
    mar = _ratio(_distance(mouth_top, mouth_bottom), _distance(mouth_left, mouth_right))
    nose = points[1]
    yaw = _ratio(nose[0] - (min_x + face_width / 2.0), face_width) * 60.0
    pitch = _ratio(nose[1] - (min_y + face_height / 2.0), face_height) * 45.0
    alerts: set[str] = set()
    if ear < ear_threshold:
        alerts.add("Eyes Closed")
    if mar > mar_threshold:
        alerts.add("Yawning")
    if abs(yaw) > yaw_threshold_deg or abs(pitch) > pitch_threshold_deg:
        alerts.add("Head Away")
    return {
        "face_detected": True,
        "ear": round(float(ear), 4),
        "mar": round(float(mar), 4),
        "yaw_deg": round(float(yaw), 2),
        "pitch_deg": round(float(pitch), 2),
        "raw_alerts": sorted(alerts),
    }


class DmsFaceAnalyzer:
    """Optional MediaPipe FaceMesh adapter with DMS reference thresholds."""

    def __init__(self, config: dict[str, Any]) -> None:
        runtime = config.get("dms", {}) or {}
        face = runtime.get("face_mesh", {}) or {}
        self.enabled = bool(face.get("enabled", True))
        self.ear_threshold = float(face.get("ear_threshold", 0.20))
        self.mar_threshold = float(face.get("mar_threshold", 0.65))
        self.yaw_threshold = float(face.get("yaw_threshold_deg", 16.0))
        self.pitch_threshold = float(face.get("pitch_threshold_deg", 14.0))
        self.pose_calibrator = NeutralPoseCalibrator(
            face.get("neutral_calibration", {}) or {}
        )
        self.error: str | None = None
        self._mesh: Any = None
        if not self.enabled:
            return
        try:
            try:
                import mediapipe as mp
            except ModuleNotFoundError:
                # LeOS provisions MediaPipe in the T-Box venv. The native
                # DeepStream venv is intentionally separate for pyds/ONNX;
                # reuse that immutable package location when present.
                tbox_sites = sorted(
                    Path("/home/letron/tbox/venv/lib").glob(
                        "python*/site-packages"
                    )
                )
                if tbox_sites:
                    sys.path.insert(0, str(tbox_sites[-1]))
                import mediapipe as mp

            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                refine_landmarks=True,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
        except Exception as exc:  # DMS remains usable with degraded face metrics.
            self.error = f"mediapipe unavailable: {exc}"
            LOG.warning("DMS FaceMesh disabled: %s", self.error)

    @property
    def available(self) -> bool:
        return self._mesh is not None

    def process(self, frame: np.ndarray) -> tuple[dict[str, Any], set[str], float]:
        started = time.perf_counter()
        metrics: dict[str, Any] = {
            "face_detected": False,
            "ear": None,
            "mar": None,
            "pose_calibrated": False,
            "pose_calibration_samples": len(self.pose_calibrator.samples),
            "raw_yaw_deg": None,
            "raw_pitch_deg": None,
            "neutral_yaw_deg": self.pose_calibrator.neutral_yaw,
            "neutral_pitch_deg": self.pose_calibrator.neutral_pitch,
            "yaw_deg": None,
            "pitch_deg": None,
        }
        if self._mesh is None or frame.size == 0:
            return metrics, set(), (time.perf_counter() - started) * 1000.0
        result = self._mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        faces = getattr(result, "multi_face_landmarks", None) or []
        if not faces:
            return metrics, set(), (time.perf_counter() - started) * 1000.0
        metrics = compute_face_metrics(
            faces[0].landmark,
            ear_threshold=self.ear_threshold,
            mar_threshold=self.mar_threshold,
            yaw_threshold_deg=self.yaw_threshold,
            pitch_threshold_deg=self.pitch_threshold,
        )
        raw_yaw = float(metrics["yaw_deg"])
        raw_pitch = float(metrics["pitch_deg"])
        raw_alerts = set(metrics.get("raw_alerts", ()))
        raw_alerts.discard("Head Away")
        pose = self.pose_calibrator.update(raw_yaw, raw_pitch)
        metrics.update(
            {
                "raw_yaw_deg": round(raw_yaw, 2),
                "raw_pitch_deg": round(raw_pitch, 2),
                **pose,
            }
        )
        if pose["pose_calibrated"] and (
            abs(float(pose["yaw_deg"])) > self.yaw_threshold
            or abs(float(pose["pitch_deg"])) > self.pitch_threshold
        ):
            raw_alerts.add("Head Away")
        metrics["raw_alerts"] = sorted(raw_alerts)
        return metrics, set(metrics.get("raw_alerts", ())), (time.perf_counter() - started) * 1000.0


class DmsBehaviorEngine:
    """Run the complete DMS policy on DeepStream-provided BGR frames."""

    def __init__(self, config: dict[str, Any]) -> None:
        runtime = config.get("dms", {}) or {}
        self.enabled = bool(runtime.get("enabled", False))
        self.interval_seconds = max(0.1, float(runtime.get("interval_ms", 200)) / 1000.0)
        self.object_detector = SmokingObjectDetector(config, section="dms")
        self.face = DmsFaceAnalyzer(config)
        self.smoother = AlertSmoother(
            int((runtime.get("alerts", {}) or {}).get("on_frames", 3)),
            int((runtime.get("alerts", {}) or {}).get("off_frames", 2)),
        )
        self.last_result = DmsInferenceResult(
            SmokingInferenceBatch((), ()), (), (), "DISABLED", {}, "not initialized"
        )
        if self.enabled:
            LOG.info(
                "DMS active: object_models=%s face_mesh=%s interval=%.0fms",
                [model.source for model in self.object_detector.models],
                self.face.available,
                self.interval_seconds * 1000.0,
            )

    @staticmethod
    def _canonical(label: str) -> str:
        return LABEL_MAP.get(label, label)

    def process(
        self,
        frame: np.ndarray,
        persons: list[tuple[int, float, float, float, float]],
        frame_number: int,
    ) -> DmsInferenceResult:
        del frame_number
        observed_ids = tuple(sorted(int(person[0]) for person in persons))
        if not self.enabled or frame.size == 0:
            return self.last_result
        started = time.perf_counter()
        person_bboxes = {
            int(track_id): (float(left), float(top), float(right), float(bottom))
            for track_id, left, top, right, bottom in persons
        }
        yolo_started = time.perf_counter()
        raw_objects = self.object_detector.process(frame)
        yolo_latency_ms = (time.perf_counter() - yolo_started) * 1000.0

        def matched_person(item: SmokingObjectDetection) -> int | None:
            matches = [
                (
                    self.object_detector.person_match_score(item, person_bbox),
                    track_id,
                )
                for track_id, person_bbox in person_bboxes.items()
            ]
            if not matches:
                return None
            quality, track_id = max(matches)
            return track_id if quality > 0.0 else None

        detections = tuple(
            DmsDetection(
                source=item.source,
                original_class=item.label,
                label=self._canonical(item.label),
                score=float(item.score),
                bbox=item.bbox,
                person_track_id=matched_person(item),
            )
            for item in raw_objects
            if self._canonical(item.label) != "Safe Driving"
        )
        raw_alerts = {item.label for item in detections if item.label in ALERT_CLASSES}
        if (
            self.object_detector.models
            and person_bboxes
            and not any(
                item.label == "Seatbelt" and item.person_track_id is not None
                for item in detections
            )
        ):
            raw_alerts.add("No Seatbelt")
        face_metrics, face_alerts, face_latency_ms = self.face.process(frame)
        raw_alerts.update(face_alerts)
        alerts = tuple(self.smoother.update(raw_alerts))

        messages: list[str] = []
        if not self.object_detector.models:
            messages.append("DMS object models unavailable")
        if self.face.enabled and not self.face.available:
            messages.append(self.face.error or "DMS FaceMesh unavailable")
        if messages:
            status = "DEGRADED" if self.object_detector.models or self.face.available else "DISABLED"
        else:
            status = "ALERT" if alerts else "OK"

        smoking_objects = [item for item in detections if item.label == "Smoking"]
        observations: list[SmokingObservation] = []
        for track_id, bbox in person_bboxes.items():
            matched = [
                item
                for item in smoking_objects
                if item.person_track_id == track_id
            ]
            if not matched:
                continue
            score = max(item.score for item in matched)
            sources = tuple(sorted({f"dms:{item.source}:{item.original_class}" for item in matched}))
            observations.append(
                SmokingObservation(
                    track_id=track_id,
                    score=score,
                    person_bbox=bbox,
                    model_roi_bbox=bbox,
                    positive=True,
                    classifier_score=0.0,
                    object_score=score,
                    signal_sources=sources,
                )
            )
        metrics = {
            **face_metrics,
            "raw_alerts": sorted(raw_alerts),
            "active_alerts": list(alerts),
            "object_detection_count": len(detections),
            "object_models_available": bool(self.object_detector.models),
            "face_mesh_available": self.face.available,
            "object_providers": self.object_detector.active_providers,
            "driver_person_count": len(person_bboxes),
            "driver_person_track_ids": sorted(person_bboxes),
            "driver_person_bboxes": [
                [round(float(value), 2) for value in person_bboxes[track_id]]
                for track_id in sorted(person_bboxes)
            ],
            "face_latency_ms": round(face_latency_ms, 2),
            "yolo_latency_ms": round(yolo_latency_ms, 2),
            "total_latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
        self.last_result = DmsInferenceResult(
            SmokingInferenceBatch(tuple(observations), observed_ids),
            detections,
            alerts,
            status,
            metrics,
            "; ".join(messages) if messages else None,
        )
        return self.last_result
