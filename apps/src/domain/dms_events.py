"""Confirmed, evidence-backed lifecycle for the DMS alert stream.

Inference output is diagnostic input, not an event. This store owns the
candidate -> confirmed -> clearing -> ended lifecycle and only publishes an
event after the label-specific confirmation policy has passed.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from domain.driver_attention import ATTENTION_EVENT_LABEL
from domain.ports import EvidencePort

BBox = tuple[float, float, float, float]

MODEL_ALERTS = frozenset(
    {
        "Distracted",
        "Smoking",
        "Drinking",
        "Eating",
        "PhoneUse",
        "Drowsy",
        "SafeDriving",
        "Seatbelt",
    }
)
ALL_ALERTS = tuple(sorted(MODEL_ALERTS | {"No Seatbelt", ATTENTION_EVENT_LABEL}))


class DmsEventState(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    CLEARING = "CLEARING"
    CLOSED = "CLOSED"


class ObservationState(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DmsAlertTransition:
    operation: str
    event_id: str
    timestamp: float
    frame_num: int
    label: str
    score: float | None
    bbox: BBox | None
    alert_sequence: int
    confirmation_state: str


@dataclass(frozen=True)
class _ConfirmationPolicy:
    confirmation_hits: int
    confirmation_window: int
    minimum_duration_seconds: float
    candidate_timeout_seconds: float
    clear_seconds: float
    unknown_timeout_seconds: float
    trace_interval_seconds: float
    min_score: float | None = None
    require_person_match: bool = False


@dataclass(frozen=True)
class _Observation:
    state: ObservationState
    evidence_type: str
    score: float | None
    bbox: BBox | None
    person_track_id: int | None
    quality: float
    metrics: dict[str, Any]
    detections: tuple[dict[str, Any], ...]


@dataclass
class _AlertEpisode:
    label: str
    candidate_since: float
    last_observed_at: float
    last_positive_at: float
    hit_history: deque[bool]
    state: DmsEventState = DmsEventState.CANDIDATE
    event_id: str | None = None
    sequence: int = 0
    confirmed_at: float | None = None
    clear_since: float | None = None
    unknown_since: float | None = None
    last_trace_at: float | None = None
    last_observation: _Observation | None = None
    best_observation: _Observation | None = None
    best_frame: np.ndarray | None = None
    best_frame_number: int = -1
    last_positive_frame: np.ndarray | None = None
    last_positive_frame_number: int = -1


class DmsAlertEventStore:
    """Own one confirmed lifecycle per DMS alert label."""

    def __init__(self, config: dict[str, Any], evidence: EvidencePort) -> None:
        runtime = config.get("dms", {}) or {}
        self.enabled = bool(runtime.get("enabled", False))
        self.camera = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.evidence = evidence
        self._policy_config = runtime.get("event_policy", {}) or {}
        face = runtime.get("face_mesh", {}) or {}
        self.ear_threshold = float(face.get("ear_threshold", 0.20))
        self.mar_threshold = float(face.get("mar_threshold", 0.65))
        self.yaw_threshold = float(face.get("yaw_threshold_deg", 16.0))
        self.pitch_threshold = float(face.get("pitch_threshold_deg", 14.0))
        self._episodes: dict[str, _AlertEpisode] = {}
        self._next_sequence: dict[str, int] = {}
        self._metrics = {
            "started_alerts": 0,
            "updated_alerts": 0,
            "ended_alerts": 0,
            "rejected_candidates": 0,
            "reacquired_alerts": 0,
            "confirmation_latencies": [],
        }

    @staticmethod
    def _slug(label: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower() or "alert"

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if np.isfinite(number) else None

    @staticmethod
    def _bbox(value: Any) -> BBox | None:
        try:
            values = tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return None
        return values if len(values) == 4 else None  # type: ignore[return-value]

    def _policy(self, label: str) -> _ConfirmationPolicy:
        if label == ATTENTION_EVENT_LABEL:
            section_name = "attention"
            defaults: dict[str, Any] = {
                "confirmation_hits": 1,
                "confirmation_window": 1,
                "minimum_duration_seconds": 0.0,
                "candidate_timeout_seconds": 1.0,
                "clear_seconds": 0.0,
                "unknown_timeout_seconds": 1.0,
                "trace_interval_ms": 1000,
                "require_person_match": True,
            }
        elif label in MODEL_ALERTS:
            section_name = "model"
            defaults = {
                "confirmation_hits": 6,
                "confirmation_window": 10,
                "minimum_duration_seconds": 1.0,
                "candidate_timeout_seconds": 3.0,
                "clear_seconds": 2.0,
                "unknown_timeout_seconds": 1.5,
                "trace_interval_ms": 1000,
                "min_score": 0.50,
                "require_person_match": True,
            }
        elif label == "No Seatbelt":
            section_name = "no_seatbelt"
            defaults = {
                "confirmation_hits": 12,
                "confirmation_window": 15,
                "minimum_duration_seconds": 2.5,
                "candidate_timeout_seconds": 4.0,
                "clear_seconds": 2.0,
                "unknown_timeout_seconds": 1.5,
                "trace_interval_ms": 1000,
            }
        else:
            section_name = "face"
            defaults = {
                "confirmation_hits": 5,
                "confirmation_window": 8,
                "minimum_duration_seconds": 0.8,
                "candidate_timeout_seconds": 2.0,
                "clear_seconds": 1.0,
                "unknown_timeout_seconds": 0.8,
                "trace_interval_ms": 800,
            }
        merged = {
            **defaults,
            **(self._policy_config.get(section_name, {}) or {}),
            **((self._policy_config.get("labels", {}) or {}).get(label, {}) or {}),
        }
        return _ConfirmationPolicy(
            confirmation_hits=max(1, int(merged["confirmation_hits"])),
            confirmation_window=max(1, int(merged["confirmation_window"])),
            minimum_duration_seconds=max(
                0.0, float(merged["minimum_duration_seconds"])
            ),
            candidate_timeout_seconds=max(
                0.1, float(merged["candidate_timeout_seconds"])
            ),
            clear_seconds=max(0.0, float(merged["clear_seconds"])),
            unknown_timeout_seconds=max(
                0.0, float(merged["unknown_timeout_seconds"])
            ),
            trace_interval_seconds=max(
                0.1, float(merged["trace_interval_ms"]) / 1000.0
            ),
            min_score=(
                max(0.0, min(1.0, float(merged["min_score"])))
                if "min_score" in merged
                else None
            ),
            require_person_match=bool(merged.get("require_person_match", False)),
        )

    @staticmethod
    def _serialize_detection(item: Any) -> dict[str, Any]:
        return {
            "label": str(getattr(item, "label", "")),
            "source": str(getattr(item, "source", "")),
            "original_class": str(getattr(item, "original_class", "")),
            "score": round(float(getattr(item, "score", 0.0)), 5),
            "bbox": [round(float(value), 2) for value in getattr(item, "bbox", ())],
            "person_track_id": getattr(item, "person_track_id", None),
        }

    @staticmethod
    def _driver_bbox(metrics: dict[str, Any]) -> BBox | None:
        boxes = [
            DmsAlertEventStore._bbox(item)
            for item in metrics.get("driver_person_bboxes", ()) or ()
        ]
        valid = [item for item in boxes if item is not None]
        if not valid:
            return None
        return max(
            valid,
            key=lambda box: max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]),
        )

    def _observation_for(
        self,
        label: str,
        result: Any,
        policy: _ConfirmationPolicy,
    ) -> _Observation:
        metrics = dict(getattr(result, "metrics", {}) or {})
        detections = tuple(getattr(result, "detections", ()) or ())
        model_available = bool(
            metrics.get("object_models_available", metrics.get("object_providers"))
        )
        person_count = int(metrics.get("driver_person_count", 0) or 0)

        if label == ATTENTION_EVENT_LABEL:
            attention = dict(metrics.get("driver_attention", {}) or {})
            event_active = attention.get("event_active") is True
            observation_ready = (
                person_count > 0
                and attention.get("state") not in {None, "no_driver", "warming"}
            )
            state = (
                ObservationState.POSITIVE
                if event_active and observation_ready
                else ObservationState.NEGATIVE
                if observation_ready
                else ObservationState.UNKNOWN
            )
            score = self._number(attention.get("score"))
            severity = (100.0 - score) / 100.0 if score is not None else 0.0
            return _Observation(
                state=state,
                evidence_type="driver_attention_policy",
                score=severity if state == ObservationState.POSITIVE else None,
                bbox=self._driver_bbox(metrics),
                person_track_id=(
                    int(metrics["driver_person_track_ids"][0])
                    if metrics.get("driver_person_track_ids")
                    else None
                ),
                quality=severity,
                metrics=metrics,
                detections=(),
            )

        if label in MODEL_ALERTS:
            matching = [item for item in detections if str(item.label) == label]
            serialized = tuple(self._serialize_detection(item) for item in matching)
            if not model_available or (policy.require_person_match and person_count <= 0):
                state = ObservationState.UNKNOWN
                best = None
            else:
                eligible = [
                    item
                    for item in matching
                    if float(item.score) >= float(policy.min_score or 0.0)
                    and (
                        not policy.require_person_match
                        or getattr(item, "person_track_id", None) is not None
                    )
                ]
                best = max(eligible, key=lambda item: float(item.score)) if eligible else None
                state = (
                    ObservationState.POSITIVE
                    if best is not None
                    else ObservationState.NEGATIVE
                )
            return _Observation(
                state=state,
                evidence_type="model_detection",
                score=float(best.score) if best is not None else None,
                bbox=self._bbox(best.bbox) if best is not None else None,
                person_track_id=(
                    getattr(best, "person_track_id", None) if best is not None else None
                ),
                quality=float(best.score) if best is not None else 0.0,
                metrics=metrics,
                detections=serialized,
            )

        if label == "No Seatbelt":
            seatbelts = [item for item in detections if str(item.label) == "Seatbelt"]
            serialized = tuple(self._serialize_detection(item) for item in seatbelts)
            if not model_available or person_count <= 0:
                state = ObservationState.UNKNOWN
            else:
                matched = [
                    item
                    for item in seatbelts
                    if getattr(item, "person_track_id", None) is not None
                ]
                state = (
                    ObservationState.NEGATIVE
                    if matched
                    else ObservationState.POSITIVE
                )
            return _Observation(
                state=state,
                evidence_type="missing_detection_rule",
                score=None,
                bbox=self._driver_bbox(metrics),
                person_track_id=(
                    int(metrics["driver_person_track_ids"][0])
                    if metrics.get("driver_person_track_ids")
                    else None
                ),
                quality=1.0 if state == ObservationState.POSITIVE else 0.0,
                metrics=metrics,
                detections=serialized,
            )

        face_detected = metrics.get("face_detected") is True
        ear = self._number(metrics.get("ear"))
        mar = self._number(metrics.get("mar"))
        yaw = self._number(metrics.get("yaw_deg"))
        pitch = self._number(metrics.get("pitch_deg"))
        pose_ready = metrics.get("pose_calibrated") is True
        if not face_detected or (label == "Head Away" and not pose_ready):
            state = ObservationState.UNKNOWN
            quality = 0.0
        elif label == "Head Away":
            yaw_ratio = abs(yaw or 0.0) / max(self.yaw_threshold, 1e-6)
            pitch_ratio = abs(pitch or 0.0) / max(self.pitch_threshold, 1e-6)
            quality = max(yaw_ratio, pitch_ratio)
            state = (
                ObservationState.POSITIVE
                if quality > 1.0
                else ObservationState.NEGATIVE
            )
        elif label == "Eyes Closed":
            quality = self.ear_threshold / max(ear or 1.0, 1e-6)
            state = (
                ObservationState.POSITIVE
                if ear is not None and ear < self.ear_threshold
                else ObservationState.NEGATIVE
            )
        else:
            quality = (mar or 0.0) / max(self.mar_threshold, 1e-6)
            state = (
                ObservationState.POSITIVE
                if mar is not None and mar > self.mar_threshold
                else ObservationState.NEGATIVE
            )
        return _Observation(
            state=state,
            evidence_type="face_pose_rule" if label == "Head Away" else "face_metric_rule",
            score=None,
            bbox=None,
            person_track_id=None,
            quality=quality,
            metrics=metrics,
            detections=(),
        )

    @staticmethod
    def _copy_frame(frame: np.ndarray | None) -> np.ndarray | None:
        return frame.copy() if frame is not None and frame.size else None

    def _remember_positive(
        self,
        episode: _AlertEpisode,
        observation: _Observation,
        frame: np.ndarray | None,
        frame_num: int,
        timestamp: float,
    ) -> None:
        episode.last_observed_at = timestamp
        episode.last_positive_at = timestamp
        episode.last_observation = observation
        episode.last_positive_frame_number = frame_num
        if (
            episode.best_observation is None
            or observation.quality > episode.best_observation.quality
        ):
            episode.best_observation = observation
            episode.best_frame = self._copy_frame(frame)
            episode.best_frame_number = frame_num
            episode.last_positive_frame = episode.best_frame

    def _new_candidate(
        self,
        label: str,
        observation: _Observation,
        policy: _ConfirmationPolicy,
        frame: np.ndarray | None,
        frame_num: int,
        timestamp: float,
    ) -> _AlertEpisode:
        episode = _AlertEpisode(
            label=label,
            candidate_since=timestamp,
            last_observed_at=timestamp,
            last_positive_at=timestamp,
            hit_history=deque([True], maxlen=policy.confirmation_window),
        )
        self._remember_positive(episode, observation, frame, frame_num, timestamp)
        self._episodes[label] = episode
        return episode

    @property
    def active_labels(self) -> list[str]:
        return sorted(
            item.label
            for item in self._episodes.values()
            if item.state in {DmsEventState.CONFIRMED, DmsEventState.CLEARING}
        )

    @property
    def candidate_labels(self) -> list[str]:
        return sorted(
            item.label
            for item in self._episodes.values()
            if item.state == DmsEventState.CANDIDATE
        )

    @property
    def active_event_ids(self) -> list[str]:
        return sorted(
            item.event_id
            for item in self._episodes.values()
            if item.event_id is not None
        )

    def _payload(
        self,
        episode: _AlertEpisode,
        *,
        state: DmsEventState | None = None,
        source_timestamp: float,
        end_reason: str | None = None,
    ) -> dict[str, Any]:
        observation = episode.last_observation or episode.best_observation
        best = episode.best_observation or observation
        metrics = dict(observation.metrics if observation is not None else {})
        metrics["active_alerts"] = self.active_labels
        metrics["candidate_alerts"] = self.candidate_labels
        current_state = state or episode.state
        policy = self._policy(episode.label)
        score = observation.score if observation is not None else None
        best_score = best.score if best is not None else None
        evidence = {
            "type": observation.evidence_type if observation else None,
            "label": episode.label,
            "observation_state": (
                observation.state.value if observation is not None else "unknown"
            ),
            "model_score": score,
            "best_score": best_score,
            "bbox": list(observation.bbox) if observation and observation.bbox else None,
            "best_bbox": list(best.bbox) if best and best.bbox else None,
            "person_track_id": observation.person_track_id if observation else None,
            "face_detected": metrics.get("face_detected"),
            "ear": metrics.get("ear"),
            "mar": metrics.get("mar"),
            "pose_calibrated": metrics.get("pose_calibrated"),
            "pose_calibration_samples": metrics.get("pose_calibration_samples"),
            "raw_yaw_deg": metrics.get("raw_yaw_deg"),
            "raw_pitch_deg": metrics.get("raw_pitch_deg"),
            "neutral_yaw_deg": metrics.get("neutral_yaw_deg"),
            "neutral_pitch_deg": metrics.get("neutral_pitch_deg"),
            "yaw_deg": metrics.get("yaw_deg"),
            "pitch_deg": metrics.get("pitch_deg"),
            "detections": list(observation.detections) if observation else [],
            "attention_score": (metrics.get("driver_attention") or {}).get("score"),
            "attention_level": (metrics.get("driver_attention") or {}).get("alert_level"),
            "attention_reasons": (metrics.get("driver_attention") or {}).get("reasons", []),
            "attention_source": (metrics.get("driver_attention") or {}).get("source"),
            "confirmation_hits": sum(episode.hit_history),
            "confirmation_window": len(episode.hit_history),
            "required_hits": policy.confirmation_hits,
            "minimum_duration_seconds": policy.minimum_duration_seconds,
        }
        payload = {
            "label": episode.label,
            "dms_alert": episode.label,
            "dms_status": current_state.value,
            "dms_alerts": [episode.label],
            "active_dms_alerts": self.active_labels,
            "dms_metrics": metrics,
            "dms_evidence": evidence,
            "alert_sequence": episode.sequence,
            "confirmation_state": current_state.value,
            "person_track_id": observation.person_track_id if observation else None,
            "detector_hits": sum(episode.hit_history),
            "observation_window": len(episode.hit_history),
            "best_bbox": list(best.bbox) if best and best.bbox else None,
            "best_frame_number": episode.best_frame_number,
            "candidate_started_at": episode.candidate_since,
            "confirmed_at": episode.confirmed_at,
            "last_positive_at": episode.last_positive_at,
            "source_timestamp": source_timestamp,
            "notification_emitted": False,
            "notification_suppressed": True,
            "score_semantics": (
                "attention_severity"
                if episode.label == ATTENTION_EVENT_LABEL
                else "model_confidence"
                if score is not None
                else "rule_evidence"
            ),
            "score": score,
            "best_score": best_score,
        }
        if end_reason is not None:
            payload["end_reason"] = end_reason
        return payload

    def _confirm(
        self,
        episode: _AlertEpisode,
        frame_num: int,
        timestamp: float,
    ) -> DmsAlertTransition:
        episode.state = DmsEventState.CONFIRMED
        episode.confirmed_at = timestamp
        episode.sequence = self._next_sequence.get(episode.label, 0) + 1
        self._next_sequence[episode.label] = episode.sequence
        episode.event_id = (
            f"dms-{self.evidence.worker_epoch}-{self._slug(episode.label)}"
            f"-{episode.sequence:04d}"
        )
        latency = timestamp - episode.candidate_since
        self._metrics["confirmation_latencies"].append(latency)
        best = episode.best_observation or episode.last_observation
        payload = self._payload(episode, source_timestamp=timestamp)
        self.evidence.start_event(
            event_id=episode.event_id,
            function="dms",
            classification=self._slug(episode.label),
            camera_id=self.camera,
            person_track_id=best.person_track_id if best else None,
            metadata=payload,
            frame=episode.best_frame,
            frame_number=episode.best_frame_number,
            bbox=best.bbox if best else None,
            score=best.score if best else None,
        )
        episode.last_trace_at = timestamp
        self._metrics["started_alerts"] += 1
        return DmsAlertTransition(
            "START",
            episode.event_id,
            timestamp,
            frame_num,
            episode.label,
            best.score if best else None,
            best.bbox if best else None,
            episode.sequence,
            episode.state.value,
        )

    def _update(
        self,
        episode: _AlertEpisode,
        frame_num: int,
        timestamp: float,
    ) -> DmsAlertTransition | None:
        if episode.event_id is None or episode.last_observation is None:
            return None
        policy = self._policy(episode.label)
        if (
            episode.last_trace_at is not None
            and timestamp - episode.last_trace_at < policy.trace_interval_seconds
        ):
            return None
        observation = episode.last_observation
        payload = self._payload(episode, source_timestamp=timestamp)
        recorded = self.evidence.record(
            episode.event_id,
            "UPDATE",
            payload,
            frame=episode.last_positive_frame,
            frame_number=frame_num,
            bbox=observation.bbox,
            score=observation.score,
        )
        if not recorded:
            return None
        episode.last_trace_at = timestamp
        self._metrics["updated_alerts"] += 1
        return DmsAlertTransition(
            "UPDATE",
            episode.event_id,
            timestamp,
            frame_num,
            episode.label,
            observation.score,
            observation.bbox,
            episode.sequence,
            episode.state.value,
        )

    def _finish(
        self,
        episode: _AlertEpisode,
        frame_num: int,
        timestamp: float,
        reason: str,
    ) -> DmsAlertTransition | None:
        event_id = episode.event_id
        if event_id is None:
            return None
        episode.state = DmsEventState.CLOSED
        observation = episode.best_observation or episode.last_observation
        payload = self._payload(
            episode,
            state=DmsEventState.CLOSED,
            source_timestamp=timestamp,
            end_reason=reason,
        )
        end_frame = (
            episode.last_positive_frame
            if episode.last_positive_frame is not None
            else episode.best_frame
        )
        self.evidence.finish_event(
            event_id,
            classification=self._slug(episode.label),
            payload=payload,
            frame=end_frame,
            frame_number=episode.last_positive_frame_number,
            bbox=observation.bbox if observation else None,
            score=observation.score if observation else None,
        )
        self._metrics["ended_alerts"] += 1
        return DmsAlertTransition(
            "END",
            event_id,
            timestamp,
            frame_num,
            episode.label,
            observation.score if observation else None,
            observation.bbox if observation else None,
            episode.sequence,
            episode.state.value,
        )

    def observe(
        self,
        *,
        frame_num: int,
        timestamp: float,
        result: Any,
        frame: np.ndarray | None,
    ) -> list[DmsAlertTransition]:
        if not self.enabled:
            return []
        transitions: list[DmsAlertTransition] = []

        for label in ALL_ALERTS:
            policy = self._policy(label)
            observation = self._observation_for(label, result, policy)
            episode = self._episodes.get(label)

            if episode is None:
                if observation.state == ObservationState.POSITIVE:
                    self._new_candidate(
                        label, observation, policy, frame, frame_num, timestamp
                    )
                continue

            if observation.state == ObservationState.POSITIVE:
                episode.hit_history.append(True)
                self._remember_positive(
                    episode, observation, frame, frame_num, timestamp
                )
                episode.unknown_since = None
                episode.clear_since = None
                if episode.state == DmsEventState.CLEARING:
                    episode.state = DmsEventState.CONFIRMED
                    self._metrics["reacquired_alerts"] += 1

                if episode.state == DmsEventState.CANDIDATE:
                    ready = (
                        sum(episode.hit_history) >= policy.confirmation_hits
                        and timestamp - episode.candidate_since + 1e-9
                        >= policy.minimum_duration_seconds
                    )
                    if ready:
                        transitions.append(
                            self._confirm(episode, frame_num, timestamp)
                        )
                elif episode.state == DmsEventState.CONFIRMED:
                    transition = self._update(episode, frame_num, timestamp)
                    if transition is not None:
                        transitions.append(transition)
                continue

            if observation.state == ObservationState.NEGATIVE:
                episode.hit_history.append(False)
                episode.last_observed_at = timestamp
                episode.unknown_since = None
                if episode.state == DmsEventState.CANDIDATE:
                    if timestamp - episode.last_positive_at >= policy.candidate_timeout_seconds:
                        self._episodes.pop(label, None)
                        self._metrics["rejected_candidates"] += 1
                    continue
                if episode.clear_since is None:
                    episode.clear_since = timestamp
                    episode.state = DmsEventState.CLEARING
                if timestamp - episode.clear_since + 1e-9 >= policy.clear_seconds:
                    self._episodes.pop(label, None)
                    transition = self._finish(
                        episode, frame_num, timestamp, "confirmed_clear"
                    )
                    if transition is not None:
                        transitions.append(transition)
                continue

            if episode.state == DmsEventState.CANDIDATE:
                if timestamp - episode.last_positive_at >= policy.candidate_timeout_seconds:
                    self._episodes.pop(label, None)
                    self._metrics["rejected_candidates"] += 1
                continue
            if episode.unknown_since is None:
                episode.unknown_since = timestamp
                episode.state = DmsEventState.CLEARING
            if timestamp - episode.unknown_since + 1e-9 >= policy.unknown_timeout_seconds:
                self._episodes.pop(label, None)
                transition = self._finish(
                    episode, frame_num, timestamp, "evidence_unavailable"
                )
                if transition is not None:
                    transitions.append(transition)

        return transitions

    def metrics(self) -> dict[str, Any]:
        latencies = self._metrics["confirmation_latencies"]
        return {
            "candidate_alerts": len(self.candidate_labels),
            "confirmed_alerts": sum(
                item.state == DmsEventState.CONFIRMED
                for item in self._episodes.values()
            ),
            "clearing_alerts": sum(
                item.state == DmsEventState.CLEARING
                for item in self._episodes.values()
            ),
            "active_alerts": len(self.active_labels),
            "candidate_labels": self.candidate_labels,
            "active_labels": self.active_labels,
            "started_alerts": self._metrics["started_alerts"],
            "updated_alerts": self._metrics["updated_alerts"],
            "ended_alerts": self._metrics["ended_alerts"],
            "rejected_candidates": self._metrics["rejected_candidates"],
            "reacquired_alerts": self._metrics["reacquired_alerts"],
            "confirmation_latency_seconds": (
                round(float(latencies[-1]), 3) if latencies else None
            ),
        }

    def close(self) -> None:
        now = time.time()
        for label, episode in list(self._episodes.items()):
            if episode.event_id is not None:
                self._finish(
                    episode,
                    episode.last_positive_frame_number,
                    now,
                    "worker_shutdown",
                )
            else:
                self._metrics["rejected_candidates"] += 1
            self._episodes.pop(label, None)
