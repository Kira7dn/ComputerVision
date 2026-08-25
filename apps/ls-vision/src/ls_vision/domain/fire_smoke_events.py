"""Spatial region tracking and temporal verification for fire/smoke events."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import cv2
import numpy as np

from ls_vision.domain.detections import FireSmokeDetection
from ls_vision.domain.ports import EvidencePort

BBox = tuple[float, float, float, float]


class ConfirmationState(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    CLEARING = "CLEARING"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class DynamicsResult:
    dynamic: bool
    changed_pixel_ratio: float = 0.0
    edge_change_ratio: float = 0.0
    flow_q75: float = 0.0
    flow_circular_variance: float = 0.0
    conditions_met: int = 0


@dataclass(frozen=True)
class TrackedFireSmokeDetection:
    label: str
    score: float
    bbox: BBox
    region_track_id: int
    confirmation_state: str


@dataclass(frozen=True)
class FireSmokeTransition:
    operation: str
    event_id: str
    label: str
    frame_num: int
    score: float
    bbox: BBox | None
    region_track_id: int
    confirmation_state: str
    confirmation_latency_seconds: float | None = None


@dataclass
class _DynamicsSample:
    crop: np.ndarray
    edges: np.ndarray
    context: np.ndarray
    context_mask: np.ndarray


class RegionDynamicsVerifier:
    """Measure local non-rigid change while discounting global exposure change."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.padding_ratio = float(config.get("crop_padding_ratio", 0.10))
        self.crop_size = int(config.get("crop_size", 96))
        self.pixel_delta = float(config.get("changed_pixel_delta", 15.0))
        self.changed_threshold = float(config.get("changed_pixel_ratio", 0.06))
        self.edge_threshold = float(config.get("edge_change_ratio", 0.04))
        self.flow_threshold = float(config.get("flow_q75", 0.5))
        self.circular_threshold = float(config.get("flow_circular_variance", 0.25))
        self.required_conditions = int(config.get("required_conditions", 2))
        self.context_padding_ratio = float(config.get("context_padding_ratio", 0.30))
        self.canny_low = int(config.get("canny_low", 50))
        self.canny_high = int(config.get("canny_high", 150))

    @staticmethod
    def _bounds(frame: np.ndarray, bbox: BBox, padding: float) -> tuple[int, int, int, int]:
        height, width = frame.shape[:2]
        left, top, right, bottom = bbox
        pad_x = max(1.0, (right - left) * padding)
        pad_y = max(1.0, (bottom - top) * padding)
        return (
            max(0, int(math.floor(left - pad_x))),
            max(0, int(math.floor(top - pad_y))),
            min(width, int(math.ceil(right + pad_x))),
            min(height, int(math.ceil(bottom + pad_y))),
        )

    @staticmethod
    def _gray(frame: np.ndarray, bounds: tuple[int, int, int, int], size: int) -> np.ndarray:
        left, top, right, bottom = bounds
        crop = frame[top:bottom, left:right]
        if crop.size == 0:
            return np.zeros((size, size), dtype=np.uint8)
        gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)

    def sample(self, frame: np.ndarray, bbox: BBox) -> _DynamicsSample:
        inner_bounds = self._bounds(frame, bbox, self.padding_ratio)
        outer_bounds = self._bounds(frame, bbox, self.context_padding_ratio)
        crop = self._gray(frame, inner_bounds, self.crop_size)
        context = self._gray(frame, outer_bounds, self.crop_size)
        mask = np.ones((self.crop_size, self.crop_size), dtype=bool)
        outer_left, outer_top, outer_right, outer_bottom = outer_bounds
        inner_left, inner_top, inner_right, inner_bottom = inner_bounds
        outer_width = max(1, outer_right - outer_left)
        outer_height = max(1, outer_bottom - outer_top)
        mask_left = int((inner_left - outer_left) * self.crop_size / outer_width)
        mask_right = int(math.ceil((inner_right - outer_left) * self.crop_size / outer_width))
        mask_top = int((inner_top - outer_top) * self.crop_size / outer_height)
        mask_bottom = int(math.ceil((inner_bottom - outer_top) * self.crop_size / outer_height))
        mask[max(0, mask_top) : min(self.crop_size, mask_bottom), max(0, mask_left) : min(self.crop_size, mask_right)] = False
        context = context.copy()
        context[~mask] = 0
        return _DynamicsSample(
            crop,
            cv2.Canny(crop, self.canny_low, self.canny_high),
            context,
            mask,
        )

    def compare(self, previous: _DynamicsSample, current: _DynamicsSample) -> DynamicsResult:
        crop_delta = cv2.absdiff(previous.crop, current.crop)
        context_delta = cv2.absdiff(previous.context, current.context)
        context_mask = previous.context_mask & current.context_mask
        context_pixels = max(1, int(np.count_nonzero(context_mask)))
        context_changed = float(
            np.count_nonzero((context_delta >= self.pixel_delta) & context_mask)
            / context_pixels
        )
        changed = max(
            0.0,
            float(np.mean(crop_delta >= self.pixel_delta))
            - context_changed,
        )
        edge_change = float(np.mean(cv2.bitwise_xor(previous.edges, current.edges) > 0))
        context_edge_delta = cv2.bitwise_xor(
            cv2.Canny(previous.context, self.canny_low, self.canny_high),
            cv2.Canny(current.context, self.canny_low, self.canny_high),
        )
        context_edge_change = float(
            np.count_nonzero((context_edge_delta > 0) & context_mask) / context_pixels
        )
        edge_change = max(0.0, edge_change - context_edge_change)
        flow = cv2.calcOpticalFlowFarneback(
            previous.crop, current.crop, None, 0.5, 3, 15, 3, 5, 1.2, 0
        )
        magnitude, angle = cv2.cartToPolar(flow[..., 0], flow[..., 1])
        flow_q75 = float(np.quantile(magnitude, 0.75))
        moving = magnitude >= max(0.05, float(np.quantile(magnitude, 0.50)))
        if np.any(moving):
            weights = magnitude[moving]
            resultant = abs(np.sum(weights * np.exp(1j * angle[moving]))) / max(
                float(np.sum(weights)), 1e-6
            )
            circular_variance = float(1.0 - resultant)
        else:
            circular_variance = 0.0
        conditions = sum(
            (
                changed >= self.changed_threshold,
                edge_change >= self.edge_threshold,
                flow_q75 >= self.flow_threshold
                and circular_variance >= self.circular_threshold,
            )
        )
        return DynamicsResult(
            conditions >= self.required_conditions,
            changed,
            edge_change,
            flow_q75,
            circular_variance,
            conditions,
        )


@dataclass
class _RegionTrack:
    track_id: int
    label: str
    raw_bbox: BBox
    smoothed_bbox: BBox
    first_seen_at: float
    last_seen_at: float
    last_frame_number: int
    score_history: deque[float]
    hit_history: deque[bool]
    dynamic_history: deque[bool]
    evidence_history: deque[tuple[float, BBox, np.ndarray | None, int, float] | None]
    state: ConfirmationState = ConfirmationState.CANDIDATE
    event_id: str | None = None
    clear_since: float | None = None
    last_trace_at: float | None = None
    last_score: float = 0.0
    previous_dynamics: _DynamicsSample | None = None
    last_dynamics: DynamicsResult = field(default_factory=lambda: DynamicsResult(False))
    best_score: float = -1.0
    best_bbox: BBox | None = None
    best_frame: np.ndarray | None = None
    best_frame_number: int = -1
    best_timestamp: float = 0.0
    last_confirmed_bbox: BBox | None = None
    last_confirmed_score: float = 0.0
    last_confirmed_frame: np.ndarray | None = None
    last_confirmed_frame_number: int = -1
    detector_ready_seen: bool = False
    notification_emitted: bool = False


class FireSmokeEventStore:
    """Track spatial regions and own one event lifecycle per confirmed region."""

    def __init__(self, config: dict[str, Any], evidence: EvidencePort) -> None:
        runtime = config.get("fire_smoke", {}) or {}
        tracking = runtime.get("tracking", {}) or {}
        dynamics = runtime.get("dynamics", {}) or {}
        self.enabled = bool(runtime.get("enabled", False))
        self.camera = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.confirmation_hits = int(tracking.get("confirmation_hits", 4))
        self.confirmation_window = int(tracking.get("confirmation_window", 6))
        self.minimum_duration = float(tracking.get("minimum_duration_seconds", 1.5))
        self.clear_seconds = float(tracking.get("clear_seconds", 3.0))
        self.match_iou = float(tracking.get("match_iou", 0.10))
        self.match_center_distance = float(tracking.get("match_center_distance", 0.20))
        self.min_area_ratio = float(tracking.get("min_area_ratio", 0.25))
        self.max_area_ratio = float(tracking.get("max_area_ratio", 4.0))
        self.smoothing_alpha = float(tracking.get("bbox_smoothing_alpha", 0.35))
        self.dynamic_votes = int(dynamics.get("confirmation_votes", 3))
        self.dynamic_window = int(dynamics.get("confirmation_window", 5))
        self.dynamics_mode = str(dynamics.get("mode", "advisory"))
        self.notification_min_duration = float(
            tracking.get("notification_min_duration_seconds", 3.0)
        )
        self.trace_interval = max(0.3, float(runtime.get("trace_interval_ms", 500)) / 1000.0)
        self.dynamics = RegionDynamicsVerifier(dynamics)
        self.evidence = evidence
        self._tracks: dict[int, _RegionTrack] = {}
        self._next_track_id = 1
        self._metrics: dict[str, Any] = {
            "rejected_static_count": 0,
            "matched_count": 0,
            "new_count": 0,
            "expired_count": 0,
            "confirmation_latency_seconds": [],
            "notification_count": 0,
            "notification_latency_seconds": [],
        }

    @staticmethod
    def _area(bbox: BBox) -> float:
        return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])

    @classmethod
    def _iou(cls, first: BBox, second: BBox) -> float:
        intersection = max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
            0.0, min(first[3], second[3]) - max(first[1], second[1])
        )
        union = cls._area(first) + cls._area(second) - intersection
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _center_distance(first: BBox, second: BBox, frame: np.ndarray | None) -> float:
        first_center = ((first[0] + first[2]) / 2.0, (first[1] + first[3]) / 2.0)
        second_center = ((second[0] + second[2]) / 2.0, (second[1] + second[3]) / 2.0)
        diagonal = (
            math.hypot(frame.shape[1], frame.shape[0])
            if frame is not None and frame.size
            else math.hypot(max(first[2], second[2], 1.0), max(first[3], second[3], 1.0))
        )
        return math.hypot(first_center[0] - second_center[0], first_center[1] - second_center[1]) / max(diagonal, 1.0)

    def _match_quality(self, track: _RegionTrack, detection: FireSmokeDetection, frame: np.ndarray | None) -> float | None:
        ratio = self._area(detection.bbox) / max(self._area(track.raw_bbox), 1e-6)
        if not self.min_area_ratio <= ratio <= self.max_area_ratio:
            return None
        overlap = self._iou(track.raw_bbox, detection.bbox)
        distance = self._center_distance(track.raw_bbox, detection.bbox, frame)
        if overlap < self.match_iou and distance > self.match_center_distance:
            return None
        return max(overlap, 1.0 - distance)

    @property
    def active_event_ids(self) -> list[str]:
        return [track.event_id for track in self._tracks.values() if track.event_id]

    @property
    def visible_detections(self) -> list[TrackedFireSmokeDetection]:
        return [
            TrackedFireSmokeDetection(
                track.label,
                track.last_confirmed_score,
                track.last_confirmed_bbox or track.smoothed_bbox,
                track.track_id,
                track.state.value,
            )
            for track in sorted(self._tracks.values(), key=lambda item: item.track_id)
            if track.state in {ConfirmationState.CONFIRMED, ConfirmationState.CLEARING}
        ]

    def metrics(self) -> dict[str, Any]:
        latencies = self._metrics["confirmation_latency_seconds"]
        return {
            "candidate_tracks": sum(track.state == ConfirmationState.CANDIDATE for track in self._tracks.values()),
            "confirmed_tracks": sum(track.state == ConfirmationState.CONFIRMED for track in self._tracks.values()),
            "clearing_tracks": sum(track.state == ConfirmationState.CLEARING for track in self._tracks.values()),
            "notification_pending_tracks": sum(
                track.state in {ConfirmationState.CONFIRMED, ConfirmationState.CLEARING}
                and not track.notification_emitted
                for track in self._tracks.values()
            ),
            "rejected_static_count": self._metrics["rejected_static_count"],
            "matched_count": self._metrics["matched_count"],
            "new_count": self._metrics["new_count"],
            "expired_count": self._metrics["expired_count"],
            "confirmation_latency_seconds": round(float(latencies[-1]), 3) if latencies else None,
            "confirmation_latency_p50_seconds": round(float(np.median(latencies)), 3) if latencies else None,
            "confirmation_latency_p95_seconds": round(float(np.quantile(latencies, 0.95)), 3) if latencies else None,
            "notification_count": self._metrics["notification_count"],
            "notification_latency_seconds": (
                round(float(self._metrics["notification_latency_seconds"][-1]), 3)
                if self._metrics["notification_latency_seconds"]
                else None
            ),
            "dynamics_mode": self.dynamics_mode,
        }

    @staticmethod
    def _refresh_best(track: _RegionTrack) -> None:
        samples = [sample for sample in track.evidence_history if sample is not None]
        if not samples:
            return
        score, bbox, frame, frame_number, timestamp = max(
            samples, key=lambda sample: sample[0]
        )
        track.best_score = score
        track.best_bbox = bbox
        track.best_frame = frame.copy() if frame is not None else None
        track.best_frame_number = frame_number
        track.best_timestamp = timestamp

    def _metadata(self, track: _RegionTrack, state: ConfirmationState | None = None) -> dict[str, Any]:
        dynamic_votes = sum(track.dynamic_history)
        return {
            "label": track.label,
            "bbox_semantics": "spatial_region_track",
            "region_track_id": track.track_id,
            "confirmation_state": (state or track.state).value,
            "detector_hits": sum(track.hit_history),
            "dynamic_votes": dynamic_votes,
            "dynamic_score": round(dynamic_votes / max(1, len(track.dynamic_history)), 5),
            "best_bbox": list(track.best_bbox) if track.best_bbox is not None else None,
            "best_frame_number": track.best_frame_number,
            "notification_emitted": track.notification_emitted,
            "notification_min_duration_seconds": self.notification_min_duration,
        }

    def _confirm(self, track: _RegionTrack, timestamp: float) -> FireSmokeTransition:
        track.state = ConfirmationState.CONFIRMED
        track.event_id = f"{track.label}-{self.evidence.worker_epoch}-region-{track.track_id:06d}"
        self._metrics["confirmation_latency_seconds"].append(timestamp - track.first_seen_at)
        bbox = track.best_bbox or track.smoothed_bbox
        frame_number = track.best_frame_number
        score = track.best_score
        latest = next(
            sample
            for sample in reversed(track.evidence_history)
            if sample is not None
        )
        (
            track.last_confirmed_score,
            track.last_confirmed_bbox,
            latest_frame,
            track.last_confirmed_frame_number,
            _latest_timestamp,
        ) = latest
        track.last_confirmed_frame = (
            latest_frame.copy() if latest_frame is not None else None
        )
        self.evidence.start_event(
            event_id=track.event_id,
            function="fire_smoke",
            classification=track.label,
            camera_id=self.camera,
            metadata={**self._metadata(track), "source_timestamp": track.best_timestamp},
            frame=track.best_frame,
            frame_number=frame_number,
            bbox=bbox,
            score=score,
        )
        track.last_trace_at = timestamp
        return FireSmokeTransition(
            "START",
            track.event_id,
            track.label,
            frame_number,
            score,
            bbox,
            track.track_id,
            track.state.value,
            timestamp - track.first_seen_at,
        )

    def _update(self, track: _RegionTrack, frame: np.ndarray | None, frame_num: int, timestamp: float) -> FireSmokeTransition | None:
        if not track.event_id:
            return None
        self.evidence.record(
            track.event_id,
            "UPDATE",
            {**self._metadata(track), "source_timestamp": timestamp},
            frame=frame,
            frame_number=frame_num,
            bbox=track.smoothed_bbox,
            score=track.last_score,
        )
        track.last_trace_at = timestamp
        return FireSmokeTransition("UPDATE", track.event_id, track.label, frame_num, track.last_score, track.smoothed_bbox, track.track_id, track.state.value)

    def _notify(self, track: _RegionTrack, frame_num: int, timestamp: float) -> FireSmokeTransition | None:
        if not track.event_id or track.notification_emitted:
            return None
        track.notification_emitted = True
        latency = timestamp - track.first_seen_at
        self._metrics["notification_count"] += 1
        self._metrics["notification_latency_seconds"].append(latency)
        recorded = self.evidence.record(
            track.event_id,
            "NOTIFY",
            {**self._metadata(track), "source_timestamp": timestamp},
            frame=None,
            frame_number=frame_num,
            bbox=track.smoothed_bbox,
            score=track.last_score,
        )
        if not recorded:
            track.notification_emitted = False
            self._metrics["notification_count"] -= 1
            self._metrics["notification_latency_seconds"].pop()
            return None
        return FireSmokeTransition(
            "NOTIFY",
            track.event_id,
            track.label,
            frame_num,
            track.last_score,
            track.smoothed_bbox,
            track.track_id,
            track.state.value,
            latency,
        )

    def _finish(self, track: _RegionTrack) -> FireSmokeTransition | None:
        if not track.event_id:
            return None
        event_id = track.event_id
        bbox = track.last_confirmed_bbox or track.smoothed_bbox
        frame_num = track.last_confirmed_frame_number
        score = track.last_confirmed_score
        self.evidence.finish_event(
            event_id,
            payload=self._metadata(track, ConfirmationState.CLOSED),
            frame=track.last_confirmed_frame,
            frame_number=frame_num,
            bbox=bbox,
            score=score,
        )
        track.state = ConfirmationState.CLOSED
        return FireSmokeTransition("END", event_id, track.label, frame_num, score, bbox, track.track_id, track.state.value)

    def _new_track(self, detection: FireSmokeDetection, frame_num: int, timestamp: float, frame: np.ndarray | None) -> _RegionTrack:
        track_id = self._next_track_id
        self._next_track_id += 1
        sample = self.dynamics.sample(frame, detection.bbox) if frame is not None and frame.size else None
        track = _RegionTrack(
            track_id,
            detection.label,
            detection.bbox,
            detection.bbox,
            timestamp,
            timestamp,
            frame_num,
            deque([detection.score], maxlen=self.confirmation_window),
            deque([True], maxlen=self.confirmation_window),
            deque(maxlen=self.dynamic_window),
            deque(
                [
                    (
                        detection.score,
                        detection.bbox,
                        frame.copy() if frame is not None else None,
                        frame_num,
                        timestamp,
                    )
                ],
                maxlen=self.confirmation_window,
            ),
            last_score=detection.score,
            previous_dynamics=sample,
            best_score=detection.score,
            best_bbox=detection.bbox,
            best_frame=frame.copy() if frame is not None else None,
            best_frame_number=frame_num,
            best_timestamp=timestamp,
        )
        self._tracks[track_id] = track
        self._metrics["new_count"] += 1
        return track

    def _observe_match(self, track: _RegionTrack, detection: FireSmokeDetection, frame_num: int, timestamp: float, frame: np.ndarray | None) -> None:
        track.raw_bbox = detection.bbox
        old = np.asarray(track.smoothed_bbox, dtype=np.float32)
        new = np.asarray(detection.bbox, dtype=np.float32)
        track.smoothed_bbox = tuple(float(value) for value in old * (1.0 - self.smoothing_alpha) + new * self.smoothing_alpha)
        track.last_seen_at = timestamp
        track.last_frame_number = frame_num
        track.last_score = detection.score
        track.hit_history.append(True)
        track.score_history.append(detection.score)
        track.evidence_history.append(
            (
                detection.score,
                track.smoothed_bbox,
                frame.copy() if frame is not None else None,
                frame_num,
                timestamp,
            )
        )
        if frame is not None and frame.size:
            sample = self.dynamics.sample(frame, track.smoothed_bbox)
            if track.previous_dynamics is not None:
                track.last_dynamics = self.dynamics.compare(track.previous_dynamics, sample)
                track.dynamic_history.append(track.last_dynamics.dynamic)
            track.previous_dynamics = sample
        self._refresh_best(track)
        if track.state == ConfirmationState.CLEARING:
            track.state = ConfirmationState.CONFIRMED
            track.clear_since = None
        if track.state in {ConfirmationState.CONFIRMED, ConfirmationState.CLEARING}:
            track.last_confirmed_bbox = track.smoothed_bbox
            track.last_confirmed_score = detection.score
            track.last_confirmed_frame = frame.copy() if frame is not None else None
            track.last_confirmed_frame_number = frame_num

    def observe(self, *, frame_num: int, timestamp: float, detections: list[FireSmokeDetection], frame: np.ndarray | None) -> list[FireSmokeTransition]:
        """Advance tracks for exactly one accepted, fresh inference result."""
        if not self.enabled:
            return []
        transitions: list[FireSmokeTransition] = []
        pairs: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            for detection_index, detection in enumerate(detections):
                if detection.label == track.label:
                    quality = self._match_quality(track, detection, frame)
                    if quality is not None:
                        pairs.append((quality, track_id, detection_index))
        matched_tracks: set[int] = set()
        matched_detections: set[int] = set()
        for _quality, track_id, detection_index in sorted(pairs, reverse=True):
            if track_id in matched_tracks or detection_index in matched_detections:
                continue
            self._observe_match(self._tracks[track_id], detections[detection_index], frame_num, timestamp, frame)
            matched_tracks.add(track_id)
            matched_detections.add(detection_index)
            self._metrics["matched_count"] += 1
        for index, detection in enumerate(detections):
            if index not in matched_detections:
                matched_tracks.add(self._new_track(detection, frame_num, timestamp, frame).track_id)

        expired: list[int] = []
        for track_id, track in sorted(self._tracks.items()):
            if track_id not in matched_tracks:
                track.hit_history.append(False)
                track.score_history.append(0.0)
                track.evidence_history.append(None)
                self._refresh_best(track)
                if track.state == ConfirmationState.CONFIRMED:
                    track.state = ConfirmationState.CLEARING
                    track.clear_since = timestamp
                if timestamp - track.last_seen_at >= self.clear_seconds:
                    if track.state == ConfirmationState.CANDIDATE and track.detector_ready_seen:
                        self._metrics["rejected_static_count"] += 1
                    transition = self._finish(track)
                    if transition is not None:
                        transitions.append(transition)
                    expired.append(track_id)
                    self._metrics["expired_count"] += 1
                    continue
            if track.state == ConfirmationState.CANDIDATE:
                detector_ready = sum(track.hit_history) >= self.confirmation_hits and timestamp - track.first_seen_at >= self.minimum_duration
                track.detector_ready_seen = track.detector_ready_seen or detector_ready
                if detector_ready:
                    transitions.append(self._confirm(track, timestamp))
            if track.state == ConfirmationState.CONFIRMED and track_id in matched_tracks:
                if (
                    not track.notification_emitted
                    and timestamp - track.first_seen_at >= self.notification_min_duration
                ):
                    transition = self._notify(track, frame_num, timestamp)
                    if transition is not None:
                        transitions.append(transition)
                if track.last_trace_at is None or timestamp - track.last_trace_at >= self.trace_interval:
                    transition = self._update(track, frame, frame_num, timestamp)
                    if transition is not None:
                        transitions.append(transition)
        for track_id in expired:
            self._tracks.pop(track_id, None)
        return transitions

    def close(self) -> None:
        for track in list(self._tracks.values()):
            self._finish(track)
        self._tracks.clear()
