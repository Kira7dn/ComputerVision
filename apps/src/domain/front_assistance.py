"""Typed camera-only front-assistance contracts and alert lifecycle."""

from __future__ import annotations

import math
import statistics
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
    probability_time: float = 0.0
    times: tuple[float, ...] = ()
    x_std: tuple[float, ...] = ()
    y_std: tuple[float, ...] = ()
    velocity_std: tuple[float, ...] = ()
    acceleration_std: tuple[float, ...] = ()


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
    lane_line_stds: tuple[tuple[tuple[float, float], ...], ...] = ()
    road_edge_stds: tuple[tuple[tuple[float, float], ...], ...] = ()
    plan_times: tuple[float, ...] = ()
    plan_velocity: tuple[tuple[float, float, float], ...] = ()
    plan_acceleration: tuple[tuple[float, float, float], ...] = ()
    plan_orientation: tuple[tuple[float, float, float], ...] = ()
    plan_orientation_rate: tuple[tuple[float, float, float], ...] = ()
    plan_stds: tuple[tuple[float, ...], ...] = ()
    pose: tuple[float, ...] = ()
    pose_stds: tuple[float, ...] = ()
    road_transform: tuple[float, ...] = ()
    road_transform_stds: tuple[float, ...] = ()
    wide_from_device_euler: tuple[float, ...] = ()
    wide_from_device_euler_stds: tuple[float, ...] = ()
    model_meta: tuple[float, ...] = ()
    desire_state: tuple[float, ...] = ()
    desire_prediction_horizons: tuple[tuple[float, ...], ...] = ()
    confidence: str = "unknown"

    def summary(self) -> dict[str, Any]:
        leads = [
            {
                "index": index,
                "probability": round(lead.probability, 5),
                "probability_time": round(lead.probability_time, 3),
                "x": round(lead.x[0], 3) if lead.x else None,
                "y": round(lead.y[0], 3) if lead.y else None,
                "velocity": round(lead.velocity[0], 3) if lead.velocity else None,
                "acceleration": (
                    round(lead.acceleration[0], 3) if lead.acceleration else None
                ),
                "x_std": round(lead.x_std[0], 3) if lead.x_std else None,
                "velocity_std": (
                    round(lead.velocity_std[0], 3) if lead.velocity_std else None
                ),
            }
            for index, lead in enumerate(self.leads)
        ]
        return {
            "contract_version": 2,
            "mode": "vision_only",
            "readiness": self.readiness.value,
            "blocking_reasons": list(self.blocking_reasons),
            "source_epoch": self.source_epoch,
            "frame_number": self.frame_number,
            "source_timestamp": self.source_timestamp,
            "lane_probabilities": [round(value, 5) for value in self.lane_probabilities],
            "lane_stds": [
                round(line[0][0], 5) if line else None for line in self.lane_line_stds
            ],
            "road_edge_stds": [
                round(edge[0][0], 5) if edge else None for edge in self.road_edge_stds
            ],
            "lead": leads[0] if leads else None,
            "leads": leads,
            "plan": {
                "horizon_seconds": round(self.plan_times[-1], 3)
                if self.plan_times
                else None,
                "point_count": len(self.path),
                "position": _first_vector(self.path),
                "velocity": _first_vector(self.plan_velocity),
                "acceleration": _first_vector(self.plan_acceleration),
                "orientation": _first_vector(self.plan_orientation),
                "orientation_rate": _first_vector(self.plan_orientation_rate),
            },
            "pose": _rounded(self.pose),
            "pose_stds": _rounded(self.pose_stds),
            "road_transform": _rounded(self.road_transform),
            "road_transform_stds": _rounded(self.road_transform_stds),
            "wide_from_device_euler": _rounded(self.wide_from_device_euler),
            "wide_from_device_euler_stds": _rounded(
                self.wide_from_device_euler_stds
            ),
            "meta": _meta_summary(self.model_meta),
            "desire_state": _rounded(self.desire_state),
            "desire_prediction_horizons": [
                _rounded(horizon) for horizon in self.desire_prediction_horizons
            ],
            "confidence": self.confidence,
            "hard_brake_predicted": self.hard_brake_predicted,
            "provider": self.provider,
            "inference_ms": round(self.inference_ms, 3),
            "model_hash": self.model_hash,
            "calibration_hash": self.calibration_hash,
            "diagnostics": dict(self.diagnostics),
        }


def _rounded(values: tuple[float, ...]) -> list[float]:
    return [round(value, 5) for value in values]


def _first_vector(
    values: tuple[tuple[float, float, float], ...],
) -> list[float] | None:
    return _rounded(values[0]) if values else None


def _meta_summary(values: tuple[float, ...]) -> dict[str, Any]:
    if len(values) != 55:
        return {"raw_probabilities": _rounded(values)}
    return {
        "engaged_probability": round(values[0], 5),
        "gas_disengage": _rounded(values[1:31:6]),
        "brake_disengage": _rounded(values[2:31:6]),
        "steer_override": _rounded(values[3:31:6]),
        "hard_brake_3": _rounded(values[4:31:6]),
        "hard_brake_4": _rounded(values[5:31:6]),
        "hard_brake_5": _rounded(values[6:31:6]),
        "gas_press": _rounded(values[31:55:4]),
        "brake_press": _rounded(values[32:55:4]),
        "left_blinker": _rounded(values[33:55:4]),
        "right_blinker": _rounded(values[34:55:4]),
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


@dataclass(frozen=True)
class _Signal:
    positive: bool
    clear_negative: bool
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VisionAlertPolicy:
    """Episode policy for advisory signals derived from camera inference only."""

    CAMERA_OFFSET = 0.04
    LANE_CLOSE_METERS = 1.08
    LANE_PROBABILITY = 0.5
    DESIRE_PROBABILITY = 0.1
    ALERT_PRIORITY = (
        "vision_fcw",
        "vision_lead_ttc",
        "vision_road_edge_left",
        "vision_road_edge_right",
        "vision_ldw_left",
        "vision_ldw_right",
        "vision_geometry_drift",
    )

    def __init__(
        self,
        *,
        confirmation_hits: int = 3,
        confirmation_window: int = 5,
        clear_negative_observations: int = 5,
        fcw_clear_observations: int = 20,
        config: dict[str, Any] | None = None,
        path_half_width_m: float = 0.9,
        max_gap_seconds: float = 0.25,
    ) -> None:
        if confirmation_hits < 1 or confirmation_window < confirmation_hits:
            raise ValueError("invalid front alert confirmation window")
        self.confirmation_hits = confirmation_hits
        self.confirmation_window = confirmation_window
        self.clear_negative_observations = max(1, clear_negative_observations)
        self.fcw_clear_observations = max(1, fcw_clear_observations)
        raw = config or {}
        self.path_half_width_m = float(path_half_width_m)
        self.max_gap_seconds = float(max_gap_seconds)
        self.ldw_lane_close_m = float(
            raw.get("ldw_lane_close_m", self.LANE_CLOSE_METERS)
        )
        self.ldw_lane_probability = float(
            raw.get("ldw_lane_probability", self.LANE_PROBABILITY)
        )
        self.ldw_desire_probability = float(
            raw.get("ldw_desire_probability", self.DESIRE_PROBABILITY)
        )
        self.ldw_confirmation_hits = int(
            raw.get("ldw_confirmation_hits", confirmation_hits)
        )
        self.ldw_confirmation_window = int(
            raw.get("ldw_confirmation_window", confirmation_window)
        )
        self.ldw_clear_observations = int(
            raw.get("ldw_clear_observations", clear_negative_observations)
        )
        self.fcw_brake_probability = float(raw.get("fcw_brake_probability", 1.0))
        self.fcw_clear_probability = float(raw.get("fcw_clear_probability", 0.0))
        self.fcw_clear_observations = int(
            raw.get("fcw_clear_observations", fcw_clear_observations)
        )
        self.lead_probability = float(raw.get("lead_probability", 0.7))
        self.lead_clear_probability = float(raw.get("lead_clear_probability", 0.5))
        self.lead_min_distance_m = float(raw.get("lead_min_distance_m", 1.0))
        self.lead_max_distance_m = float(raw.get("lead_max_distance_m", 80.0))
        self.lead_min_closing_speed_mps = float(
            raw.get("lead_min_closing_speed_mps", 1.0)
        )
        self.lead_ttc_seconds = float(raw.get("lead_ttc_seconds", 3.0))
        self.lead_clear_ttc_seconds = float(raw.get("lead_clear_ttc_seconds", 4.0))
        self.lead_confirmation_hits = int(raw.get("lead_confirmation_hits", 3))
        self.lead_confirmation_window = int(raw.get("lead_confirmation_window", 5))
        self.lead_clear_observations = int(raw.get("lead_clear_observations", 10))
        self.edge_min_x_m = float(raw.get("edge_min_x_m", 5.0))
        self.edge_max_x_m = float(raw.get("edge_max_x_m", 30.0))
        self.edge_max_std_m = float(raw.get("edge_max_std_m", 0.6))
        self.edge_trigger_clearance_m = float(
            raw.get("edge_trigger_clearance_m", 0.25)
        )
        self.edge_clear_clearance_m = float(raw.get("edge_clear_clearance_m", 0.5))
        self.edge_confirmation_hits = int(raw.get("edge_confirmation_hits", 3))
        self.edge_confirmation_window = int(raw.get("edge_confirmation_window", 5))
        self.edge_clear_observations = int(raw.get("edge_clear_observations", 10))
        self.geometry_baseline_frames = int(raw.get("geometry_baseline_frames", 200))
        self.geometry_roll_pitch_deg = float(raw.get("geometry_roll_pitch_deg", 2.0))
        self.geometry_yaw_deg = float(raw.get("geometry_yaw_deg", 3.0))
        self.geometry_translation_m = float(raw.get("geometry_translation_m", 0.25))
        self.geometry_trigger_hits = int(raw.get("geometry_trigger_hits", 40))
        self.geometry_trigger_window = int(raw.get("geometry_trigger_window", 50))
        self.geometry_clear_observations = int(
            raw.get("geometry_clear_observations", 100)
        )
        self._validate_thresholds()
        self._windows = {
            "vision_ldw_left": deque(maxlen=self.ldw_confirmation_window),
            "vision_ldw_right": deque(maxlen=self.ldw_confirmation_window),
            "vision_lead_ttc": deque(maxlen=self.lead_confirmation_window),
            "vision_road_edge_left": deque(maxlen=self.edge_confirmation_window),
            "vision_road_edge_right": deque(maxlen=self.edge_confirmation_window),
            "vision_geometry_drift": deque(maxlen=self.geometry_trigger_window),
        }
        self._confirmation_hits = {
            "vision_ldw_left": self.ldw_confirmation_hits,
            "vision_ldw_right": self.ldw_confirmation_hits,
            "vision_lead_ttc": self.lead_confirmation_hits,
            "vision_road_edge_left": self.edge_confirmation_hits,
            "vision_road_edge_right": self.edge_confirmation_hits,
            "vision_geometry_drift": self.geometry_trigger_hits,
        }
        self._clear_after = {
            "vision_ldw_left": self.ldw_clear_observations,
            "vision_ldw_right": self.ldw_clear_observations,
            "vision_fcw": self.fcw_clear_observations,
            "vision_lead_ttc": self.lead_clear_observations,
            "vision_road_edge_left": self.edge_clear_observations,
            "vision_road_edge_right": self.edge_clear_observations,
            "vision_geometry_drift": self.geometry_clear_observations,
        }
        self._active: dict[str, str] = {}
        self._negative_counts: dict[str, int] = {}
        self._sequences: dict[str, int] = {}
        self._epoch: str | None = None
        self._last_timestamp: float | None = None
        self._geometry_samples: list[tuple[float, ...]] = []
        self._geometry_baseline: tuple[float, ...] | None = None
        self._geometry_diagnostics: dict[str, Any] = {"baseline_ready": False}

    def _validate_thresholds(self) -> None:
        valid = (
            0.0 <= self.path_half_width_m
            and self.ldw_lane_close_m > 0.0
            and 0.0 <= self.ldw_lane_probability <= 1.0
            and 0.0 <= self.ldw_desire_probability <= 1.0
            and 1 <= self.ldw_confirmation_hits <= self.ldw_confirmation_window
            and self.ldw_clear_observations >= 1
            and 0.0 <= self.fcw_clear_probability < self.fcw_brake_probability <= 1.0
            and self.fcw_clear_observations >= 1
            and 0.0 <= self.lead_clear_probability < self.lead_probability <= 1.0
            and 0.0 < self.lead_min_distance_m < self.lead_max_distance_m
            and self.lead_min_closing_speed_mps > 0.0
            and 0.0 < self.lead_ttc_seconds < self.lead_clear_ttc_seconds
            and 1 <= self.lead_confirmation_hits <= self.lead_confirmation_window
            and self.lead_clear_observations >= 1
            and 0.0 < self.edge_min_x_m < self.edge_max_x_m
            and self.edge_max_std_m > 0.0
            and 0.0 <= self.edge_trigger_clearance_m < self.edge_clear_clearance_m
            and 1 <= self.edge_confirmation_hits <= self.edge_confirmation_window
            and self.edge_clear_observations >= 1
            and self.geometry_baseline_frames >= 1
            and self.geometry_roll_pitch_deg > 0.0
            and self.geometry_yaw_deg > 0.0
            and self.geometry_translation_m > 0.0
            and 1 <= self.geometry_trigger_hits <= self.geometry_trigger_window
            and self.geometry_clear_observations >= 1
            and self.max_gap_seconds > 0.0
        )
        if not valid:
            raise ValueError("invalid front assistance alert thresholds")

    @property
    def active_labels(self) -> tuple[str, ...]:
        return tuple(label for label in self.ALERT_PRIORITY if label in self._active)

    @property
    def banner_label(self) -> str | None:
        return self.active_labels[0] if self.active_labels else None

    @property
    def geometry_diagnostics(self) -> dict[str, Any]:
        return dict(self._geometry_diagnostics)

    def reset(self, source_epoch: str | None = None) -> None:
        for window in self._windows.values():
            window.clear()
        self._active.clear()
        self._negative_counts.clear()
        self._epoch = source_epoch
        self._last_timestamp = None
        self._geometry_samples.clear()
        self._geometry_baseline = None
        self._geometry_diagnostics = {"baseline_ready": False}

    def _raw_signals(self, perception: FrontPerception) -> dict[str, _Signal]:
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
            left_probability > self.ldw_lane_probability
            and left_y > -(self.ldw_lane_close_m + self.CAMERA_OFFSET)
            and left_desire > self.ldw_desire_probability
        )
        right = (
            right_probability > self.ldw_lane_probability
            and right_y < (self.ldw_lane_close_m - self.CAMERA_OFFSET)
            and right_desire > self.ldw_desire_probability
        )
        fcw_score = max(perception.hard_brake_3_probs, default=0.0)
        fcw = perception.hard_brake_predicted or fcw_score >= self.fcw_brake_probability
        fcw_clear = (
            not perception.hard_brake_predicted
            and fcw_score <= self.fcw_clear_probability
        )
        signals = {
            "vision_ldw_left": _Signal(
                left,
                not left,
                min(left_probability, left_desire),
                {"lane_probability": left_probability, "desire_probability": left_desire},
            ),
            "vision_ldw_right": _Signal(
                right,
                not right,
                min(right_probability, right_desire),
                {"lane_probability": right_probability, "desire_probability": right_desire},
            ),
            "vision_fcw": _Signal(
                fcw,
                fcw_clear,
                1.0 if perception.hard_brake_predicted else fcw_score,
                {
                    "fcw_brake_probability": fcw_score,
                    "hard_brake_3_probs": list(perception.hard_brake_3_probs),
                    "hard_brake_5_probs": list(perception.hard_brake_5_probs),
                },
            ),
        }
        signals["vision_lead_ttc"] = self._lead_signal(perception)
        signals.update(self._road_edge_signals(perception))
        geometry_signal = self._geometry_signal(perception)
        self._geometry_diagnostics = dict(geometry_signal.metadata)
        signals["vision_geometry_drift"] = geometry_signal
        return signals

    def _lead_signal(self, perception: FrontPerception) -> _Signal:
        if not perception.leads:
            return _Signal(False, True, 0.0)
        lead = perception.leads[0]
        if not lead.x or not lead.velocity:
            return _Signal(False, True, 0.0)
        distance = lead.x[0]
        ego_velocity = (
            perception.plan_velocity[0][0] if perception.plan_velocity else 0.0
        )
        velocity_closing = max(0.0, ego_velocity - lead.velocity[0])
        trajectory_closing = 0.0
        if len(lead.x) >= 2:
            times = lead.times if len(lead.times) >= 2 else (0.0, 2.0)
            delta_t = times[1] - times[0]
            if delta_t > 0.0:
                trajectory_closing = max(0.0, (lead.x[0] - lead.x[1]) / delta_t)
        closing_speed = max(velocity_closing, trajectory_closing)
        ttc = distance / closing_speed if closing_speed > 1e-6 else math.inf
        finite = all(math.isfinite(value) for value in (distance, closing_speed, ttc))
        positive = (
            finite
            and lead.probability >= self.lead_probability
            and self.lead_min_distance_m <= distance <= self.lead_max_distance_m
            and closing_speed >= self.lead_min_closing_speed_mps
            and ttc <= self.lead_ttc_seconds
        )
        clear = (
            not finite
            or lead.probability < self.lead_clear_probability
            or ttc >= self.lead_clear_ttc_seconds
        )
        return _Signal(
            positive,
            clear,
            lead.probability if positive else 0.0,
            {
                "lead_probability": lead.probability,
                "distance_m": distance,
                "ego_velocity_mps": ego_velocity,
                "lead_velocity_mps": lead.velocity[0],
                "closing_speed_mps": closing_speed,
                "closing_speed_source": (
                    "lead_trajectory"
                    if trajectory_closing > velocity_closing
                    else "relative_velocity"
                ),
                "ttc_seconds": None if not math.isfinite(ttc) else ttc,
            },
        )

    def _road_edge_signals(self, perception: FrontPerception) -> dict[str, _Signal]:
        result: dict[str, _Signal] = {}
        for side, edge_index in (("left", 0), ("right", 1)):
            label = f"vision_road_edge_{side}"
            if edge_index >= len(perception.road_edges):
                result[label] = _Signal(False, True, 0.0)
                continue
            edge = perception.road_edges[edge_index]
            edge_stds = (
                perception.road_edge_stds[edge_index]
                if edge_index < len(perception.road_edge_stds)
                else ()
            )
            clearances: list[float] = []
            accepted_stds: list[float] = []
            for index, (path, edge_point) in enumerate(zip(perception.path, edge, strict=False)):
                x = path[0]
                if not self.edge_min_x_m <= x <= self.edge_max_x_m:
                    continue
                std = edge_stds[index][0] if index < len(edge_stds) else math.inf
                if not math.isfinite(std) or std > self.edge_max_std_m:
                    continue
                path_boundary = path[1] + (-self.path_half_width_m if side == "left" else self.path_half_width_m)
                clearance = (
                    path_boundary - edge_point[1]
                    if side == "left"
                    else edge_point[1] - path_boundary
                )
                if math.isfinite(clearance):
                    clearances.append(clearance)
                    accepted_stds.append(std)
            if not clearances:
                result[label] = _Signal(False, False, 0.0, {"sample_count": 0})
                continue
            clearance = min(clearances)
            edge_std = max(accepted_stds)
            result[label] = _Signal(
                clearance <= self.edge_trigger_clearance_m,
                clearance >= self.edge_clear_clearance_m,
                max(0.0, min(1.0, 1.0 - edge_std / self.edge_max_std_m)),
                {
                    "clearance_m": clearance,
                    "edge_std_m": edge_std,
                    "sample_count": len(clearances),
                },
            )
        return result

    def _geometry_signal(self, perception: FrontPerception) -> _Signal:
        if len(perception.wide_from_device_euler) < 3 or len(perception.road_transform) < 3:
            return _Signal(False, False, 0.0, {"baseline_ready": False})
        sample = (*perception.wide_from_device_euler[:3], *perception.road_transform[:3])
        if not all(math.isfinite(value) for value in sample):
            return _Signal(False, True, 0.0, {"baseline_ready": False})
        if self._geometry_baseline is None:
            self._geometry_samples.append(sample)
            if len(self._geometry_samples) < self.geometry_baseline_frames:
                return _Signal(
                    False,
                    False,
                    0.0,
                    {
                        "baseline_ready": False,
                        "baseline_samples": len(self._geometry_samples),
                    },
                )
            self._geometry_baseline = tuple(
                statistics.median(values) for values in zip(*self._geometry_samples, strict=True)
            )
            self._geometry_samples.clear()
        baseline = self._geometry_baseline
        angular_deg = tuple(
            abs(math.degrees(value - reference))
            for value, reference in zip(sample[:3], baseline[:3], strict=True)
        )
        translation_delta = math.sqrt(
            sum((value - reference) ** 2 for value, reference in zip(sample[3:], baseline[3:], strict=True))
        )
        positive = (
            angular_deg[0] > self.geometry_roll_pitch_deg
            or angular_deg[1] > self.geometry_roll_pitch_deg
            or angular_deg[2] > self.geometry_yaw_deg
            or translation_delta > self.geometry_translation_m
        )
        clear = (
            angular_deg[0] < self.geometry_roll_pitch_deg * 0.6
            and angular_deg[1] < self.geometry_roll_pitch_deg * 0.6
            and angular_deg[2] < self.geometry_yaw_deg * 0.6
            and translation_delta < self.geometry_translation_m * 0.6
        )
        ratios = (
            angular_deg[0] / self.geometry_roll_pitch_deg,
            angular_deg[1] / self.geometry_roll_pitch_deg,
            angular_deg[2] / self.geometry_yaw_deg,
            translation_delta / self.geometry_translation_m,
        )
        return _Signal(
            positive,
            clear,
            min(1.0, max(ratios)),
            {
                "experimental_advisory": True,
                "baseline_ready": True,
                "mounting_delta_deg": list(angular_deg),
                "road_translation_delta_m": translation_delta,
            },
        )

    def observe(self, perception: FrontPerception) -> list[FrontAlertTransition]:
        discontinuity = (
            self._last_timestamp is not None
            and (
                perception.source_timestamp <= self._last_timestamp
                or perception.source_timestamp - self._last_timestamp > self.max_gap_seconds
            )
        )
        if self._epoch != perception.source_epoch or discontinuity:
            self.reset(perception.source_epoch)
        self._last_timestamp = perception.source_timestamp
        if not perception.valid or perception.readiness is not FrontReadiness.READY:
            raw = {
                label: _Signal(False, True, 0.0, {"readiness": perception.readiness.value})
                for label in (*self._windows, "vision_fcw")
            }
            raw["vision_geometry_drift"] = _Signal(
                False,
                True,
                0.0,
                {
                    "readiness": perception.readiness.value,
                    "experimental_advisory": True,
                },
            )
        else:
            raw = self._raw_signals(perception)

        transitions: list[FrontAlertTransition] = []
        for label, signal in raw.items():
            if label in self._windows:
                window = self._windows[label]
                window.append(signal.positive)
                confirmed = signal.positive if label in self._active else (
                    len(window) == window.maxlen
                    and sum(window) >= self._confirmation_hits[label]
                )
            else:
                confirmed = signal.positive

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
                            signal.confidence,
                            {"mode": "vision_only", **signal.metadata},
                        )
                    )
                continue

            if label not in self._active:
                continue
            if not signal.clear_negative:
                continue
            negative_count = self._negative_counts.get(label, 0) + 1
            self._negative_counts[label] = negative_count
            clear_after = self._clear_after[label]
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
                        {"mode": "vision_only", **signal.metadata},
                    )
                )
        return transitions
