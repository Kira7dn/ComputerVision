"""Typed camera-only front-assistance contracts and alert lifecycle."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FrontReadiness(str, Enum):
    WARMING = "warming"
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"


@dataclass(frozen=True)
class FrontCalibration:
    """Provisioned camera geometry used to build the openpilot model warp."""

    profile_id: str
    source_width: int
    source_height: int
    intrinsics: tuple[tuple[float, float, float], ...]
    rpy_calib: tuple[float, float, float] = (0.0, 0.0, 0.0)
    artifact_hash: str = ""
    valid: bool = True


@dataclass(frozen=True)
class FrontLead:
    probability: float
    x: tuple[float, ...]
    y: tuple[float, ...]
    velocity: tuple[float, ...]
    acceleration: tuple[float, ...]


@dataclass(frozen=True)
class FrontPerception:
    source_epoch: str
    frame_number: int
    source_timestamp: float
    valid: bool
    readiness: FrontReadiness
    blocking_reasons: tuple[str, ...]
    lane_lines: tuple[tuple[tuple[float, float, float], ...], ...]
    lane_probabilities: tuple[float, ...]
    road_edges: tuple[tuple[tuple[float, float, float], ...], ...]
    path: tuple[tuple[float, float, float], ...]
    leads: tuple[FrontLead, ...]
    desire_prediction: tuple[float, ...]
    hard_brake_predicted: bool
    hard_brake_3_probs: tuple[float, ...]
    hard_brake_5_probs: tuple[float, ...]
    provider: str
    inference_ms: float
    model_hash: str
    calibration_hash: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        lead = self.leads[0] if self.leads else None
        return {
            "contract_version": 1,
            "mode": "vision_only",
            "readiness": self.readiness.value,
            "blocking_reasons": list(self.blocking_reasons),
            "source_epoch": self.source_epoch,
            "frame_number": self.frame_number,
            "source_timestamp": self.source_timestamp,
            "lane_probabilities": [round(value, 5) for value in self.lane_probabilities],
            "lead": (
                {
                    "probability": round(lead.probability, 5),
                    "x": round(lead.x[0], 3) if lead.x else None,
                    "y": round(lead.y[0], 3) if lead.y else None,
                    "velocity": round(lead.velocity[0], 3) if lead.velocity else None,
                }
                if lead is not None
                else None
            ),
            "hard_brake_predicted": self.hard_brake_predicted,
            "provider": self.provider,
            "inference_ms": round(self.inference_ms, 3),
            "model_hash": self.model_hash,
            "calibration_hash": self.calibration_hash,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class FrontAlertTransition:
    operation: str
    event_id: str
    label: str
    frame_number: int
    source_timestamp: float
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VisionAlertPolicy:
    """Episode policy for advisory signals derived from camera inference only."""

    CAMERA_OFFSET = 0.04
    LANE_CLOSE_METERS = 1.08
    LANE_PROBABILITY = 0.5
    DESIRE_PROBABILITY = 0.1

    def __init__(
        self,
        *,
        confirmation_hits: int = 3,
        confirmation_window: int = 5,
        clear_negative_observations: int = 5,
        fcw_clear_observations: int = 20,
    ) -> None:
        if confirmation_hits < 1 or confirmation_window < confirmation_hits:
            raise ValueError("invalid front alert confirmation window")
        self.confirmation_hits = confirmation_hits
        self.confirmation_window = confirmation_window
        self.clear_negative_observations = max(1, clear_negative_observations)
        self.fcw_clear_observations = max(1, fcw_clear_observations)
        self._windows = {
            "vision_ldw_left": deque(maxlen=confirmation_window),
            "vision_ldw_right": deque(maxlen=confirmation_window),
        }
        self._active: dict[str, str] = {}
        self._negative_counts: dict[str, int] = {}
        self._sequences: dict[str, int] = {}
        self._epoch: str | None = None

    @property
    def active_labels(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    def reset(self, source_epoch: str | None = None) -> None:
        for window in self._windows.values():
            window.clear()
        self._active.clear()
        self._negative_counts.clear()
        self._epoch = source_epoch

    def _raw_signals(self, perception: FrontPerception) -> dict[str, tuple[bool, float]]:
        probabilities = perception.lane_probabilities
        desire = perception.desire_prediction
        lanes = perception.lane_lines
        left_y = lanes[1][0][1] if len(lanes) > 1 and lanes[1] else float("-inf")
        right_y = lanes[2][0][1] if len(lanes) > 2 and lanes[2] else float("inf")
        left_probability = probabilities[1] if len(probabilities) > 1 else 0.0
        right_probability = probabilities[2] if len(probabilities) > 2 else 0.0
        left_desire = desire[1] if len(desire) > 1 else 0.0
        right_desire = desire[2] if len(desire) > 2 else 0.0
        left = (
            left_probability > self.LANE_PROBABILITY
            and left_y > -(self.LANE_CLOSE_METERS + self.CAMERA_OFFSET)
            and left_desire > self.DESIRE_PROBABILITY
        )
        right = (
            right_probability > self.LANE_PROBABILITY
            and right_y < (self.LANE_CLOSE_METERS - self.CAMERA_OFFSET)
            and right_desire > self.DESIRE_PROBABILITY
        )
        return {
            "vision_ldw_left": (left, min(left_probability, left_desire)),
            "vision_ldw_right": (right, min(right_probability, right_desire)),
            "vision_fcw": (perception.hard_brake_predicted, 1.0 if perception.hard_brake_predicted else 0.0),
        }

    def observe(self, perception: FrontPerception) -> list[FrontAlertTransition]:
        if self._epoch != perception.source_epoch:
            self.reset(perception.source_epoch)
        if not perception.valid or perception.readiness is not FrontReadiness.READY:
            raw = {label: (False, 0.0) for label in (*self._windows, "vision_fcw")}
        else:
            raw = self._raw_signals(perception)

        transitions: list[FrontAlertTransition] = []
        for label, (positive, confidence) in raw.items():
            if label in self._windows:
                window = self._windows[label]
                window.append(positive)
                confirmed = len(window) == window.maxlen and sum(window) >= self.confirmation_hits
            else:
                confirmed = positive

            if confirmed:
                self._negative_counts[label] = 0
                if label not in self._active:
                    sequence = self._sequences.get(label, 0) + 1
                    self._sequences[label] = sequence
                    event_id = f"front-{perception.source_epoch}-{label}-{sequence}"
                    self._active[label] = event_id
                    transitions.append(
                        FrontAlertTransition(
                            "START",
                            event_id,
                            label,
                            perception.frame_number,
                            perception.source_timestamp,
                            confidence,
                            {"mode": "vision_only"},
                        )
                    )
                continue

            if label not in self._active:
                continue
            negative_count = self._negative_counts.get(label, 0) + 1
            self._negative_counts[label] = negative_count
            clear_after = (
                self.fcw_clear_observations
                if label == "vision_fcw"
                else self.clear_negative_observations
            )
            if negative_count >= clear_after:
                event_id = self._active.pop(label)
                self._negative_counts.pop(label, None)
                transitions.append(
                    FrontAlertTransition(
                        "END",
                        event_id,
                        label,
                        perception.frame_number,
                        perception.source_timestamp,
                        0.0,
                        {"mode": "vision_only"},
                    )
                )
        return transitions
