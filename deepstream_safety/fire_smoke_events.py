"""Temporal lifecycle for full-frame fire/smoke detections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

try:
    from .evidence import EvidenceStore
    from .fire_smoke_engine import FireSmokeDetection
except ImportError:  # pipeline.py is also executed as a standalone script
    from evidence import EvidenceStore
    from fire_smoke_engine import FireSmokeDetection


@dataclass(frozen=True)
class FireSmokeTransition:
    operation: str
    event_id: str
    label: str
    frame_num: int
    score: float
    bbox: tuple[float, float, float, float] | None


@dataclass
class _ClassState:
    label: str
    scores: list[float] = field(default_factory=list)
    event_id: str | None = None
    candidate_since: float | None = None
    clear_since: float | None = None
    last_score: float = 0.0
    last_bbox: tuple[float, float, float, float] | None = None
    last_trace_at: float | None = None


class FireSmokeEventStore:
    """Aggregate each class into its own camera-level lifecycle event."""

    def __init__(self, config: dict[str, Any], evidence: EvidenceStore) -> None:
        runtime = config.get("fire_smoke", {}) or {}
        event_cfg = config.get("fire_smoke_events", {}) or {}
        self.enabled = bool(runtime.get("enabled", False))
        self.camera = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.confirmation_hits = max(1, int(event_cfg.get("confirmation_hits", runtime.get("confirmation_hits", 2))))
        self.confirmation_window = max(
            self.confirmation_hits,
            int(event_cfg.get("confirmation_window", runtime.get("confirmation_window", 4))),
        )
        self.clear_seconds = float(event_cfg.get("clear_seconds", runtime.get("clear_seconds", 3.0)))
        self.trace_interval = max(0.3, float(event_cfg.get("trace_interval_ms", runtime.get("trace_interval_ms", 500))) / 1000.0)
        self._states = {label: _ClassState(label) for label in ("fire", "smoke")}
        self.evidence = evidence

    @property
    def active_event_ids(self) -> list[str]:
        return [state.event_id for state in self._states.values() if state.event_id]

    def _record(
        self,
        state: _ClassState,
        operation: str,
        *,
        frame_num: int,
        timestamp: float,
        frame: np.ndarray | None,
    ) -> FireSmokeTransition | None:
        if not state.event_id:
            return None
        self.evidence.record(
            state.event_id,
            operation,
            {
                "label": state.label,
                "bbox_semantics": "camera_level_detection",
                "source_timestamp": timestamp,
            },
            frame=frame,
            frame_number=frame_num,
            bbox=state.last_bbox,
            score=state.last_score,
            force_image=operation in {"START", "END"},
        )
        state.last_trace_at = timestamp
        return FireSmokeTransition(
            operation,
            state.event_id,
            state.label,
            frame_num,
            state.last_score,
            state.last_bbox,
        )

    def _finish(
        self,
        state: _ClassState,
        *,
        frame_num: int,
        frame: np.ndarray | None,
    ) -> FireSmokeTransition | None:
        if not state.event_id:
            return None
        event_id = state.event_id
        self.evidence.finish_event(
            event_id,
            payload={"label": state.label},
            frame=frame,
            frame_number=frame_num,
            bbox=state.last_bbox,
            score=state.last_score,
        )
        return FireSmokeTransition("END", event_id, state.label, frame_num, state.last_score, state.last_bbox)

    def observe(
        self,
        *,
        frame_num: int,
        timestamp: float,
        detections: list[FireSmokeDetection],
        frame: np.ndarray | None,
    ) -> list[FireSmokeTransition]:
        if not self.enabled:
            return []
        transitions: list[FireSmokeTransition] = []
        best_by_label: dict[str, FireSmokeDetection] = {}
        for detection in detections:
            current = best_by_label.get(detection.label)
            if current is None or detection.score > current.score:
                best_by_label[detection.label] = detection

        for label, state in self._states.items():
            detection = best_by_label.get(label)
            if detection is None:
                if state.event_id:
                    state.clear_since = state.clear_since or timestamp
                    if timestamp - state.clear_since >= self.clear_seconds:
                        transition = self._finish(state, frame_num=frame_num, frame=frame)
                        if transition is not None:
                            transitions.append(transition)
                        self._states[label] = _ClassState(label)
                elif state.candidate_since is not None and timestamp - state.candidate_since >= self.clear_seconds:
                    self._states[label] = _ClassState(label)
                continue

            state.clear_since = None
            state.candidate_since = state.candidate_since or timestamp
            state.scores.append(detection.score)
            del state.scores[:-self.confirmation_window]
            state.last_score = detection.score
            state.last_bbox = detection.bbox
            if state.event_id is None and len([score for score in state.scores if score > 0]) >= self.confirmation_hits:
                state.event_id = (
                    f"{label}-{self.evidence.worker_epoch}-{uuid.uuid4().hex[:24]}"
                )
                self.evidence.start_event(
                    event_id=state.event_id,
                    function="fire_smoke",
                    classification=label,
                    camera_id=self.camera,
                    metadata={
                        "label": label,
                        "bbox_semantics": "camera_level_detection",
                    },
                    frame=frame,
                    frame_number=frame_num,
                    bbox=state.last_bbox,
                    score=state.last_score,
                )
                state.last_trace_at = timestamp
                transitions.append(FireSmokeTransition("START", state.event_id, label, frame_num, state.last_score, state.last_bbox))
            elif state.event_id and (state.last_trace_at is None or timestamp - state.last_trace_at >= self.trace_interval):
                transition = self._record(state, "UPDATE", frame_num=frame_num, timestamp=timestamp, frame=frame)
                if transition is not None:
                    transitions.append(transition)
        return transitions

    def close(self) -> None:
        for state in self._states.values():
            self._finish(state, frame_num=-1, frame=None)
        self._states = {label: _ClassState(label) for label in self._states}
