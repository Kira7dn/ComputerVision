"""Per-person smoking observations and episode lifecycle."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from ls_vision.domain.ports import EvidencePort

BBox = tuple[float, float, float, float]


class SmokingState(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    CLEARING = "CLEARING"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class SmokingObservation:
    track_id: int
    score: float
    person_bbox: BBox
    model_roi_bbox: BBox
    positive: bool | None = None
    classifier_score: float | None = None
    object_score: float | None = None
    signal_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SmokingInferenceBatch:
    observations: tuple[SmokingObservation, ...]
    observed_track_ids: tuple[int, ...]
    invalid_crop_track_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SmokingDetection:
    track_id: int
    score: float
    person_bbox: BBox
    model_roi_bbox: BBox
    episode_sequence: int
    confirmation_state: str

    @property
    def box(self) -> np.ndarray:
        return np.array([*self.person_bbox, self.score], dtype=np.float32)


@dataclass(frozen=True)
class SmokingTransition:
    operation: str
    event_id: str
    timestamp: float
    frame_num: int
    score: float
    bbox: BBox | None
    person_track_id: int
    episode_sequence: int
    confirmation_state: str
    latency_seconds: float | None = None


EvidenceSample = tuple[
    float,
    BBox,
    BBox,
    np.ndarray | None,
    int,
    float,
    float | None,
    float | None,
    tuple[str, ...],
]


@dataclass
class _Episode:
    track_id: int
    episode_sequence: int
    candidate_since: float
    last_observed_at: float
    hit_history: deque[bool]
    score_history: deque[float]
    evidence_history: deque[EvidenceSample]
    state: SmokingState = SmokingState.CANDIDATE
    event_id: str | None = None
    clear_since: float | None = None
    negative_streak: int = 0
    last_score: float = 0.0
    last_person_bbox: BBox | None = None
    last_model_roi_bbox: BBox | None = None
    last_classifier_score: float | None = None
    last_object_score: float | None = None
    last_signal_sources: tuple[str, ...] = ()
    last_trace_at: float | None = None
    notification_emitted: bool = False
    best_score: float = -1.0
    best_person_bbox: BBox | None = None
    best_model_roi_bbox: BBox | None = None
    best_frame: np.ndarray | None = None
    best_frame_number: int = -1
    best_timestamp: float = 0.0
    best_classifier_score: float | None = None
    best_object_score: float | None = None
    best_signal_sources: tuple[str, ...] = ()
    last_confirmed_score: float = 0.0
    last_confirmed_person_bbox: BBox | None = None
    last_confirmed_model_roi_bbox: BBox | None = None
    last_confirmed_frame: np.ndarray | None = None
    last_confirmed_frame_number: int = -1


class SmokingEpisodeStore:
    """Own the complete temporal lifecycle for each tracked person's episode."""

    def __init__(self, config: dict[str, Any], evidence: EvidencePort) -> None:
        runtime = config.get("smoking_behavior", {}) or {}
        temporal = runtime.get("temporal", {}) or {}
        lifecycle = runtime.get("lifecycle", {}) or {}
        self.enabled = bool(runtime.get("enabled", False))
        self.camera = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.threshold = float(runtime.get("smoking_threshold", 0.60))
        self.confirmation_hits = int(temporal.get("confirmation_hits", 2))
        self.confirmation_window = int(temporal.get("confirmation_window", 4))
        self.minimum_duration = float(temporal.get("minimum_duration_seconds", 0.4))
        self.clear_negative_observations = int(
            temporal.get("clear_negative_observations", 4)
        )
        self.candidate_timeout = float(lifecycle.get("candidate_timeout_seconds", 3.0))
        self.clearing_seconds = float(lifecycle.get("clearing_seconds", 3.0))
        self.notification_min_duration = float(
            lifecycle.get("notification_min_duration_seconds", 3.0)
        )
        self.trace_interval = max(
            0.3, float(lifecycle.get("trace_interval_ms", 400)) / 1000.0
        )
        self.evidence = evidence
        self._episodes: dict[int, _Episode] = {}
        self._next_episode_by_track: dict[int, int] = {}
        self._metrics: dict[str, Any] = {
            "positive_observations": 0,
            "negative_observations": 0,
            "invalid_crop_observations": 0,
            "new_episodes": 0,
            "reacquired_episodes": 0,
            "closed_episodes": 0,
            "notification_count": 0,
            "confirmation_latencies": [],
            "notification_latencies": [],
        }

    @property
    def state(self) -> SmokingState | None:
        if any(item.state == SmokingState.CONFIRMED for item in self._episodes.values()):
            return SmokingState.CONFIRMED
        if any(item.state == SmokingState.CLEARING for item in self._episodes.values()):
            return SmokingState.CLEARING
        if self._episodes:
            return SmokingState.CANDIDATE
        return None

    @property
    def active_event_id(self) -> str | None:
        active = [item for item in self._episodes.values() if item.event_id]
        return max(active, key=lambda item: item.last_score).event_id if active else None

    @property
    def active_event_ids(self) -> list[str]:
        return [item.event_id for item in self._episodes.values() if item.event_id]

    @property
    def visible_detections(self) -> list[SmokingDetection]:
        return [
            SmokingDetection(
                item.track_id,
                item.last_confirmed_score,
                item.last_person_bbox or item.last_confirmed_person_bbox or (0.0, 0.0, 0.0, 0.0),
                item.last_confirmed_model_roi_bbox or item.last_model_roi_bbox or (0.0, 0.0, 0.0, 0.0),
                item.episode_sequence,
                item.state.value,
            )
            for item in sorted(self._episodes.values(), key=lambda value: value.track_id)
            if item.state in {SmokingState.CONFIRMED, SmokingState.CLEARING}
        ]

    def metrics(self) -> dict[str, Any]:
        confirmation = self._metrics["confirmation_latencies"]
        notification = self._metrics["notification_latencies"]
        return {
            "candidate_episodes": sum(item.state == SmokingState.CANDIDATE for item in self._episodes.values()),
            "confirmed_episodes": sum(item.state == SmokingState.CONFIRMED for item in self._episodes.values()),
            "clearing_episodes": sum(item.state == SmokingState.CLEARING for item in self._episodes.values()),
            "notification_pending_episodes": sum(
                item.event_id is not None and not item.notification_emitted
                for item in self._episodes.values()
            ),
            "positive_observations": self._metrics["positive_observations"],
            "negative_observations": self._metrics["negative_observations"],
            "invalid_crop_observations": self._metrics["invalid_crop_observations"],
            "new_episodes": self._metrics["new_episodes"],
            "reacquired_episodes": self._metrics["reacquired_episodes"],
            "closed_episodes": self._metrics["closed_episodes"],
            "notification_count": self._metrics["notification_count"],
            "confirmation_latency_seconds": round(float(confirmation[-1]), 3) if confirmation else None,
            "notification_latency_seconds": round(float(notification[-1]), 3) if notification else None,
        }

    def _new_episode(
        self,
        observation: SmokingObservation,
        frame_num: int,
        timestamp: float,
        frame: np.ndarray | None,
    ) -> _Episode:
        sequence = self._next_episode_by_track.get(observation.track_id, 0) + 1
        self._next_episode_by_track[observation.track_id] = sequence
        sample: EvidenceSample = (
            observation.score,
            observation.person_bbox,
            observation.model_roi_bbox,
            frame.copy() if frame is not None else None,
            frame_num,
            timestamp,
            observation.classifier_score,
            observation.object_score,
            observation.signal_sources,
        )
        item = _Episode(
            track_id=observation.track_id,
            episode_sequence=sequence,
            candidate_since=timestamp,
            last_observed_at=timestamp,
            hit_history=deque([True], maxlen=self.confirmation_window),
            score_history=deque([observation.score], maxlen=self.confirmation_window),
            evidence_history=deque([sample], maxlen=self.confirmation_window),
            last_score=observation.score,
            last_person_bbox=observation.person_bbox,
            last_model_roi_bbox=observation.model_roi_bbox,
            last_classifier_score=observation.classifier_score,
            last_object_score=observation.object_score,
            last_signal_sources=observation.signal_sources,
        )
        self._refresh_best(item)
        self._episodes[observation.track_id] = item
        self._metrics["new_episodes"] += 1
        return item

    @staticmethod
    def _refresh_best(item: _Episode) -> None:
        if not item.evidence_history:
            return
        (
            score,
            person_bbox,
            roi_bbox,
            frame,
            frame_num,
            timestamp,
            classifier_score,
            object_score,
            signal_sources,
        ) = max(
            item.evidence_history, key=lambda sample: sample[0]
        )
        item.best_score = score
        item.best_person_bbox = person_bbox
        item.best_model_roi_bbox = roi_bbox
        item.best_frame = frame.copy() if frame is not None else None
        item.best_frame_number = frame_num
        item.best_timestamp = timestamp
        item.best_classifier_score = classifier_score
        item.best_object_score = object_score
        item.best_signal_sources = signal_sources

    def _metadata(self, item: _Episode, state: SmokingState | None = None) -> dict[str, Any]:
        return {
            "label": "smoking",
            "person_track_id": item.track_id,
            "episode_sequence": item.episode_sequence,
            "confirmation_state": (state or item.state).value,
            "positive_votes": sum(item.hit_history),
            "observation_window": len(item.hit_history),
            "best_score": item.best_score,
            "best_person_bbox": list(item.best_person_bbox) if item.best_person_bbox else None,
            "best_model_roi_bbox": list(item.best_model_roi_bbox) if item.best_model_roi_bbox else None,
            "person_bbox": list(item.last_person_bbox) if item.last_person_bbox else None,
            "model_roi_bbox": list(item.last_model_roi_bbox) if item.last_model_roi_bbox else None,
            "best_frame_number": item.best_frame_number,
            "classifier_score": item.last_classifier_score,
            "object_score": item.last_object_score,
            "signal_sources": list(item.last_signal_sources),
            "best_classifier_score": item.best_classifier_score,
            "best_object_score": item.best_object_score,
            "best_signal_sources": list(item.best_signal_sources),
            "notification_emitted": item.notification_emitted,
            "notification_min_duration_seconds": self.notification_min_duration,
        }

    def _confirm(self, item: _Episode, timestamp: float) -> SmokingTransition:
        item.state = SmokingState.CONFIRMED
        item.event_id = (
            f"smoking-{self.evidence.worker_epoch}-person-{item.track_id}"
            f"-episode-{item.episode_sequence:04d}"
        )
        latency = timestamp - item.candidate_since
        self._metrics["confirmation_latencies"].append(latency)
        latest = item.evidence_history[-1]
        (
            item.last_confirmed_score,
            item.last_confirmed_person_bbox,
            item.last_confirmed_model_roi_bbox,
            latest_frame,
            item.last_confirmed_frame_number,
            _latest_timestamp,
            _latest_classifier_score,
            _latest_object_score,
            _latest_signal_sources,
        ) = latest
        item.last_confirmed_frame = latest_frame.copy() if latest_frame is not None else None
        self.evidence.start_event(
            event_id=item.event_id,
            function="smoking_behavior",
            classification="smoking",
            camera_id=self.camera,
            person_track_id=item.track_id,
            metadata={**self._metadata(item), "source_timestamp": item.best_timestamp},
            frame=item.best_frame,
            frame_number=item.best_frame_number,
            bbox=item.best_person_bbox,
            score=item.best_score,
        )
        item.last_trace_at = timestamp
        return SmokingTransition(
            "START", item.event_id, timestamp, item.best_frame_number, item.best_score,
            item.best_person_bbox, item.track_id, item.episode_sequence, item.state.value, latency,
        )

    def _record(
        self,
        item: _Episode,
        operation: str,
        frame_num: int,
        timestamp: float,
        frame: np.ndarray | None,
    ) -> bool:
        if not item.event_id:
            return False
        recorded = self.evidence.record(
            item.event_id,
            operation,
            {**self._metadata(item), "source_timestamp": timestamp},
            frame=frame if operation == "UPDATE" else None,
            frame_number=frame_num,
            bbox=item.last_person_bbox,
            score=item.last_score,
        )
        if recorded and operation == "UPDATE":
            item.last_trace_at = timestamp
        return recorded

    def _notify(self, item: _Episode, frame_num: int, timestamp: float) -> SmokingTransition | None:
        if not item.event_id or item.notification_emitted:
            return None
        item.notification_emitted = True
        if not self._record(item, "NOTIFY", frame_num, timestamp, None):
            item.notification_emitted = False
            return None
        latency = timestamp - item.candidate_since
        self._metrics["notification_count"] += 1
        self._metrics["notification_latencies"].append(latency)
        return SmokingTransition(
            "NOTIFY", item.event_id, timestamp, frame_num, item.last_confirmed_score,
            item.last_confirmed_person_bbox, item.track_id, item.episode_sequence,
            item.state.value, latency,
        )

    def _finish(
        self, item: _Episode, timestamp: float, frame_num: int
    ) -> SmokingTransition | None:
        if not item.event_id:
            return None
        event_id = item.event_id
        self.evidence.finish_event(
            event_id,
            payload={
                **self._metadata(item, SmokingState.CLOSED),
                "source_timestamp": timestamp,
            },
            frame=item.last_confirmed_frame,
            frame_number=item.last_confirmed_frame_number,
            bbox=item.last_confirmed_person_bbox,
            score=item.last_confirmed_score,
        )
        item.state = SmokingState.CLOSED
        self._metrics["closed_episodes"] += 1
        return SmokingTransition(
            "END", event_id, timestamp, frame_num,
            item.last_confirmed_score, item.last_confirmed_person_bbox, item.track_id,
            item.episode_sequence, item.state.value,
        )

    def observe(
        self,
        *,
        frame_num: int,
        timestamp: float,
        observations: list[SmokingObservation],
        observed_track_ids: set[int],
        frame: np.ndarray | None,
        invalid_crop_track_ids: set[int] | None = None,
    ) -> list[SmokingTransition]:
        if not self.enabled:
            return []
        self._metrics["invalid_crop_observations"] += len(invalid_crop_track_ids or ())
        transitions: list[SmokingTransition] = []
        by_track = {observation.track_id: observation for observation in observations}
        for track_id, observation in sorted(by_track.items()):
            positive = (
                observation.positive
                if observation.positive is not None
                else observation.score >= self.threshold
            )
            self._metrics[
                "positive_observations" if positive else "negative_observations"
            ] += 1
            item = self._episodes.get(track_id)
            if item is None:
                if not positive:
                    continue
                item = self._new_episode(observation, frame_num, timestamp, frame)
            else:
                item.last_observed_at = timestamp
                item.last_score = observation.score
                item.last_person_bbox = observation.person_bbox
                item.last_model_roi_bbox = observation.model_roi_bbox
                item.last_classifier_score = observation.classifier_score
                item.last_object_score = observation.object_score
                item.last_signal_sources = observation.signal_sources
                item.hit_history.append(positive)
                item.score_history.append(observation.score)
                item.evidence_history.append(
                    (
                        observation.score,
                        observation.person_bbox,
                        observation.model_roi_bbox,
                        frame.copy() if frame is not None else None,
                        frame_num,
                        timestamp,
                        observation.classifier_score,
                        observation.object_score,
                        observation.signal_sources,
                    )
                )
                self._refresh_best(item)

            if positive:
                item.negative_streak = 0
                if item.state == SmokingState.CLEARING:
                    item.state = SmokingState.CONFIRMED
                    item.clear_since = None
                    self._metrics["reacquired_episodes"] += 1
                if item.state == SmokingState.CONFIRMED:
                    item.last_confirmed_score = observation.score
                    item.last_confirmed_person_bbox = observation.person_bbox
                    item.last_confirmed_model_roi_bbox = observation.model_roi_bbox
                    item.last_confirmed_frame = frame.copy() if frame is not None else None
                    item.last_confirmed_frame_number = frame_num
            elif item.state == SmokingState.CONFIRMED:
                item.negative_streak += 1
                if item.negative_streak >= self.clear_negative_observations:
                    item.state = SmokingState.CLEARING
                    item.clear_since = timestamp

            if item.state == SmokingState.CANDIDATE:
                ready = (
                    sum(item.hit_history) >= self.confirmation_hits
                    and timestamp - item.candidate_since + 1e-9 >= self.minimum_duration
                )
                if ready:
                    transitions.append(self._confirm(item, timestamp))

            if item.state == SmokingState.CONFIRMED:
                if (
                    not item.notification_emitted
                    and timestamp - item.candidate_since >= self.notification_min_duration
                ):
                    transition = self._notify(item, frame_num, timestamp)
                    if transition is not None:
                        transitions.append(transition)
                if item.last_trace_at is None or timestamp - item.last_trace_at >= self.trace_interval:
                    if self._record(item, "UPDATE", frame_num, timestamp, frame):
                        transitions.append(
                            SmokingTransition(
                                "UPDATE", item.event_id or "", timestamp, frame_num,
                                item.last_score, item.last_person_bbox, item.track_id,
                                item.episode_sequence, item.state.value,
                            )
                        )

        expired: list[int] = []
        for track_id, item in sorted(self._episodes.items()):
            if track_id not in observed_track_ids and item.state == SmokingState.CONFIRMED:
                item.state = SmokingState.CLEARING
                item.clear_since = timestamp
            if item.state == SmokingState.CANDIDATE:
                if timestamp - item.candidate_since >= self.candidate_timeout:
                    expired.append(track_id)
            elif item.state == SmokingState.CLEARING:
                item.clear_since = item.clear_since or timestamp
                if timestamp - item.clear_since >= self.clearing_seconds:
                    transition = self._finish(item, timestamp, frame_num)
                    if transition is not None:
                        transitions.append(transition)
                    expired.append(track_id)
        for track_id in expired:
            self._episodes.pop(track_id, None)
        return transitions

    def close(self) -> None:
        for item in list(self._episodes.values()):
            self._finish(item, item.last_observed_at, item.last_confirmed_frame_number)
        self._episodes.clear()
