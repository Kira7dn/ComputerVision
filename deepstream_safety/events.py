"""Per-person smoking event lifecycle; EvidenceStore owns durable artifacts."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

try:
    from .evidence import EvidenceStore
except ImportError:  # pipeline.py is also executed as a standalone script
    from evidence import EvidenceStore


class EventState(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    ACTIVE = "active"


@dataclass(frozen=True)
class SafetyDetection:
    track_id: int
    score: float
    bbox: tuple[float, float, float, float]
    model_roi_bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class EventTransition:
    operation: str
    event_id: str
    timestamp: float
    frame_num: int
    score: float
    bbox: tuple[float, float, float, float] | None
    person_track_id: int | None


@dataclass
class _TrackState:
    track_id: int
    candidate_since: float
    clear_since: float | None = None
    event_id: str | None = None
    last_score: float = 0.0
    last_bbox: tuple[float, float, float, float] | None = None
    last_model_roi_bbox: tuple[float, float, float, float] | None = None
    last_trace_at: float | None = None


class SafetyEventStore:
    """Keep an independent pending/active lifecycle for every person track."""

    def __init__(self, config: dict[str, Any], evidence: EvidenceStore) -> None:
        event_cfg = config.get("events", {}) or {}
        self.enabled = bool(event_cfg.get("enabled", True))
        self.camera = str(
            event_cfg.get("camera")
            or (config.get("input", {}) or {}).get("camera", "camera")
        )
        self.function = "smoking_behavior"
        self.confirm_seconds = float(event_cfg.get("confirm_seconds", 1.0))
        self.clear_seconds = float(event_cfg.get("clear_seconds", 5.0))
        self.trace_interval = max(
            0.3, float(event_cfg.get("trace_interval_ms", 400)) / 1000.0
        )
        self.evidence = evidence
        self._tracks: dict[int, _TrackState] = {}

    @property
    def state(self) -> EventState:
        if any(item.event_id for item in self._tracks.values()):
            return EventState.ACTIVE
        if self._tracks:
            return EventState.PENDING
        return EventState.IDLE

    @property
    def active_event_id(self) -> str | None:
        active = [item for item in self._tracks.values() if item.event_id]
        if not active:
            return None
        return max(active, key=lambda item: item.last_score).event_id

    @property
    def active_event_ids(self) -> list[str]:
        return [item.event_id for item in self._tracks.values() if item.event_id]

    def _record(
        self,
        item: _TrackState,
        operation: str,
        *,
        frame_num: int,
        timestamp: float,
        detection: SafetyDetection | None,
        frame: np.ndarray | None,
    ) -> None:
        if not item.event_id:
            return
        score = detection.score if detection else item.last_score
        bbox = detection.bbox if detection else item.last_bbox
        self.evidence.record(
            item.event_id,
            operation,
            {
                "label": "smoking",
                "person_track_id": item.track_id,
                "source_timestamp": timestamp,
                "person_bbox": list(bbox) if bbox is not None else None,
                "model_roi_bbox": (
                    list(detection.model_roi_bbox)
                    if detection is not None and detection.model_roi_bbox is not None
                    else None
                ),
            },
            frame=frame,
            frame_number=frame_num,
            bbox=bbox,
            score=score,
            force_image=operation in {"START", "END"},
        )
        item.last_trace_at = timestamp

    def _finish(
        self,
        item: _TrackState,
        *,
        frame_num: int,
        frame: np.ndarray | None,
    ) -> EventTransition | None:
        if not item.event_id:
            return None
        event_id = item.event_id
        self.evidence.finish_event(
            event_id,
            payload={
                "label": "smoking",
                "person_track_id": item.track_id,
                "person_bbox": list(item.last_bbox) if item.last_bbox is not None else None,
                "model_roi_bbox": (
                    list(item.last_model_roi_bbox)
                    if item.last_model_roi_bbox is not None
                    else None
                ),
            },
            frame=frame,
            frame_number=frame_num,
            bbox=item.last_bbox,
            score=item.last_score,
        )
        return EventTransition(
            "END",
            event_id,
            time.time(),
            frame_num,
            item.last_score,
            item.last_bbox,
            item.track_id,
        )

    def observe(
        self,
        frame_num: int,
        timestamp: float,
        detections: list[SafetyDetection],
        frame: np.ndarray | None = None,
    ) -> EventTransition | None:
        if not self.enabled:
            return None
        transitions: list[EventTransition] = []
        seen: set[int] = set()
        for detection in detections:
            seen.add(detection.track_id)
            item = self._tracks.get(detection.track_id)
            if item is None:
                item = _TrackState(detection.track_id, timestamp)
                self._tracks[detection.track_id] = item
            item.clear_since = None
            item.last_score = detection.score
            item.last_bbox = detection.bbox
            item.last_model_roi_bbox = detection.model_roi_bbox
            if item.event_id is None:
                if timestamp - item.candidate_since >= self.confirm_seconds:
                    item.event_id = (
                        f"smoking-{self.evidence.worker_epoch}-{uuid.uuid4().hex[:24]}"
                    )
                    self.evidence.start_event(
                        event_id=item.event_id,
                        function=self.function,
                        classification="smoking",
                        camera_id=self.camera,
                        person_track_id=item.track_id,
                        metadata={
                            "label": "smoking",
                            "person_bbox": list(detection.bbox),
                            "model_roi_bbox": (
                                list(detection.model_roi_bbox)
                                if detection.model_roi_bbox is not None
                                else None
                            ),
                        },
                        frame=frame,
                        frame_number=frame_num,
                        bbox=detection.bbox,
                        score=detection.score,
                    )
                    item.last_trace_at = timestamp
                    transitions.append(
                        EventTransition(
                            "START",
                            item.event_id,
                            timestamp,
                            frame_num,
                            detection.score,
                            detection.bbox,
                            detection.track_id,
                        )
                    )
            elif item.last_trace_at is None or timestamp - item.last_trace_at >= self.trace_interval:
                self._record(
                    item,
                    "UPDATE",
                    frame_num=frame_num,
                    timestamp=timestamp,
                    detection=detection,
                    frame=frame,
                )
                transitions.append(
                    EventTransition(
                        "UPDATE",
                        item.event_id,
                        timestamp,
                        frame_num,
                        detection.score,
                        detection.bbox,
                        detection.track_id,
                    )
                )

        for track_id, item in list(self._tracks.items()):
            if track_id in seen:
                continue
            item.clear_since = item.clear_since or timestamp
            if timestamp - item.clear_since < self.clear_seconds:
                continue
            transition = self._finish(item, frame_num=frame_num, frame=frame)
            if transition is not None:
                transitions.append(transition)
            self._tracks.pop(track_id, None)

        if not transitions:
            return None
        return max(transitions, key=lambda item: (item.operation == "END", item.timestamp))

    def close(self) -> None:
        for item in list(self._tracks.values()):
            self._finish(item, frame_num=-1, frame=None)
        self._tracks.clear()
