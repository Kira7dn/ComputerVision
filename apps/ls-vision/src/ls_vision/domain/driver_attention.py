"""Camera-only driver-attention policy inspired by openpilot monitoring.

The policy is deliberately independent from a model/runtime adapter.  It owns
time-based awareness, alert escalation and recovery while callers provide
tri-state observations from FaceMesh/Soham or the openpilot cabin model.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

ATTENTION_EVENT_LABEL = "Driver Inattention"
ATTENTION_REASONS = ("pose", "eyes", "phone", "fatigue", "no_face", "uncertain")


@dataclass(frozen=True)
class AttentionObservation:
    timestamp: float
    driver_present: bool
    face_detected: bool
    pose: bool | None = None
    eyes: bool | None = None
    phone: bool | None = None
    fatigue: bool | None = None
    uncertain: bool = False
    source: str = "current"
    confidence: float | None = None


@dataclass(frozen=True)
class DriverAttentionState:
    readiness: str
    state: str
    score: int | None
    alert_level: str
    reasons: tuple[str, ...]
    source: str
    event_active: bool
    attentive: bool
    updated_at: float

    def summary(self) -> dict[str, Any]:
        return {
            "contract_version": 1,
            "readiness": self.readiness,
            "state": self.state,
            "score": self.score,
            "alert_level": self.alert_level,
            "reasons": list(self.reasons),
            "source": self.source,
            "event_active": self.event_active,
            "attentive": self.attentive,
            "updated_at": self.updated_at,
        }


class NeutralPoseCalibrator:
    """Estimate a camera/driver-specific straight-ahead pose from stable samples."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", True))
        self.minimum_samples = max(3, int(config.get("minimum_samples", 15)))
        self.minimum_duration_seconds = max(
            0.0, float(config.get("minimum_duration_seconds", 0.0))
        )
        self.window_size = max(
            self.minimum_samples, int(config.get("window_size", 30))
        )
        self.max_yaw_std = max(0.1, float(config.get("max_yaw_std_deg", 8.0)))
        self.max_pitch_std = max(0.1, float(config.get("max_pitch_std_deg", 5.0)))
        self.neutral_update_alpha = max(
            0.0, min(1.0, float(config.get("neutral_update_alpha", 0.01)))
        )
        self.neutral_update_max_delta = max(
            0.1, float(config.get("neutral_update_max_delta_deg", 5.0))
        )
        self.samples: deque[tuple[float, float]] = deque(maxlen=self.window_size)
        self.neutral_yaw: float | None = None
        self.neutral_pitch: float | None = None
        self.first_sample_at: float | None = None
        self.last_sample_at: float | None = None

    @property
    def calibrated(self) -> bool:
        return not self.enabled or (
            self.neutral_yaw is not None and self.neutral_pitch is not None
        )

    @property
    def calibrated_percent(self) -> int:
        if self.calibrated:
            return 100
        sample_progress = len(self.samples) / max(self.minimum_samples, 1)
        if self.minimum_duration_seconds > 0 and self.first_sample_at is not None:
            duration = max(0.0, float(self.last_sample_at or self.first_sample_at) - self.first_sample_at)
            duration_progress = duration / self.minimum_duration_seconds
            sample_progress = min(sample_progress, duration_progress)
        return int(max(0.0, min(1.0, sample_progress)) * 100)

    def update(
        self,
        yaw: float,
        pitch: float,
        *,
        timestamp: float | None = None,
        sample_valid: bool = True,
    ) -> dict[str, Any]:
        if not self.enabled:
            return self._result(yaw, pitch, True)

        if sample_valid and self.neutral_yaw is None:
            self.samples.append((yaw, pitch))
            if timestamp is not None:
                if self.first_sample_at is None:
                    self.first_sample_at = timestamp
                self.last_sample_at = timestamp
            duration_ready = self.minimum_duration_seconds <= 0.0 or (
                self.first_sample_at is not None
                and self.last_sample_at is not None
                and self.last_sample_at - self.first_sample_at >= self.minimum_duration_seconds
            )
            if len(self.samples) >= self.minimum_samples and duration_ready:
                values = np.asarray(self.samples, dtype=np.float32)
                if (
                    float(np.std(values[:, 0])) <= self.max_yaw_std
                    and float(np.std(values[:, 1])) <= self.max_pitch_std
                ):
                    self.neutral_yaw = float(np.median(values[:, 0]))
                    self.neutral_pitch = float(np.median(values[:, 1]))

        if not self.calibrated:
            return self._result(yaw, pitch, False)

        yaw_delta = yaw - float(self.neutral_yaw or 0.0)
        pitch_delta = pitch - float(self.neutral_pitch or 0.0)
        if (
            sample_valid
            and abs(yaw_delta) <= self.neutral_update_max_delta
            and abs(pitch_delta) <= self.neutral_update_max_delta
            and self.neutral_update_alpha > 0.0
        ):
            alpha = self.neutral_update_alpha
            self.neutral_yaw = (1.0 - alpha) * float(self.neutral_yaw) + alpha * yaw
            self.neutral_pitch = (1.0 - alpha) * float(self.neutral_pitch) + alpha * pitch
            yaw_delta = yaw - float(self.neutral_yaw)
            pitch_delta = pitch - float(self.neutral_pitch)
        return self._result(yaw_delta, pitch_delta, True, raw=(yaw, pitch))

    def _result(
        self,
        yaw: float,
        pitch: float,
        calibrated: bool,
        *,
        raw: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        raw_yaw, raw_pitch = raw or (yaw, pitch)
        return {
            "pose_calibrated": calibrated,
            "pose_calibration_samples": len(self.samples),
            "pose_calibrated_percent": self.calibrated_percent,
            "neutral_yaw_deg": round(float(self.neutral_yaw), 2) if self.neutral_yaw is not None else None,
            "neutral_pitch_deg": round(float(self.neutral_pitch), 2) if self.neutral_pitch is not None else None,
            "raw_yaw_deg": round(float(raw_yaw), 2),
            "raw_pitch_deg": round(float(raw_pitch), 2),
            "yaw_deg": round(float(yaw), 2) if calibrated else None,
            "pitch_deg": round(float(pitch), 2) if calibrated else None,
        }


class DriverAttentionPolicy:
    """Time-based awareness budget with openpilot-compatible alert timing."""

    def __init__(self, config: dict[str, Any]) -> None:
        alert_seconds = list(config.get("alert_seconds", (5.0, 8.0, 13.0)))
        if len(alert_seconds) != 3:
            alert_seconds = [5.0, 8.0, 13.0]
        self.warning_seconds, self.critical_seconds, self.emergency_seconds = (
            max(0.1, float(item)) for item in alert_seconds
        )
        self.recovery_confirm_seconds = max(
            0.0, float(config.get("recovery_confirm_seconds", 2.0))
        )
        self.max_step_seconds = max(0.1, float(config.get("max_step_seconds", 1.0)))
        self.recovery_factor_min = max(1.0, float(config.get("recovery_factor_min", 1.25)))
        self.recovery_factor_max = max(
            self.recovery_factor_min, float(config.get("recovery_factor_max", 5.0))
        )
        self.awareness = 1.0
        self.last_timestamp: float | None = None
        self.attentive_since: float | None = None
        self.event_latched = False

    def reset(self, timestamp: float, *, source: str, readiness: str = "ready") -> DriverAttentionState:
        self.awareness = 1.0
        self.last_timestamp = timestamp
        self.attentive_since = None
        self.event_latched = False
        return DriverAttentionState(
            readiness, "no_driver", None, "none", (), source, False, False, timestamp
        )

    def update(self, observation: AttentionObservation, *, readiness: str = "ready") -> DriverAttentionState:
        timestamp = float(observation.timestamp)
        if not observation.driver_present:
            return self.reset(timestamp, source=observation.source, readiness=readiness)

        elapsed = 0.0 if self.last_timestamp is None else timestamp - self.last_timestamp
        self.last_timestamp = timestamp
        elapsed = max(0.0, min(self.max_step_seconds, elapsed))

        reasons: list[str] = []
        if not observation.face_detected:
            reasons.append("no_face")
        if observation.uncertain:
            reasons.append("uncertain")
        for name in ("pose", "eyes", "phone", "fatigue"):
            if getattr(observation, name) is True:
                reasons.append(name)

        values = (observation.pose, observation.eyes, observation.phone, observation.fatigue)
        has_known_signal = any(value is not None for value in values)
        attentive = (
            observation.face_detected
            and not observation.uncertain
            and has_known_signal
            and not reasons
        )
        distracted_or_unknown = bool(reasons) or not has_known_signal

        if distracted_or_unknown:
            self.awareness = max(-0.1, self.awareness - elapsed / self.emergency_seconds)
            self.attentive_since = None
        elif attentive:
            recovery = (
                (self.recovery_factor_max - self.recovery_factor_min)
                * (1.0 - self.awareness)
                + self.recovery_factor_min
            )
            self.awareness = min(1.0, self.awareness + recovery * elapsed / self.emergency_seconds)
            if self.attentive_since is None:
                self.attentive_since = timestamp

        warning_threshold = 1.0 - self.warning_seconds / self.emergency_seconds
        critical_threshold = 1.0 - self.critical_seconds / self.emergency_seconds
        if self.awareness <= 0.0:
            alert_level = "emergency"
        elif self.awareness <= critical_threshold:
            alert_level = "critical"
        elif self.awareness <= warning_threshold:
            alert_level = "warning"
        else:
            alert_level = "none"

        if alert_level in {"critical", "emergency"}:
            self.event_latched = True
        elif self.event_latched and attentive:
            recovered_for = timestamp - float(self.attentive_since or timestamp)
            if recovered_for >= self.recovery_confirm_seconds and self.awareness > warning_threshold:
                self.event_latched = False

        state = "attentive" if attentive else "distracted" if reasons else "unknown"
        return DriverAttentionState(
            readiness=readiness,
            state=state,
            score=int(round(max(0.0, min(1.0, self.awareness)) * 100.0)),
            alert_level=alert_level,
            reasons=tuple(name for name in ATTENTION_REASONS if name in reasons),
            source=observation.source,
            event_active=self.event_latched,
            attentive=attentive,
            updated_at=timestamp,
        )
