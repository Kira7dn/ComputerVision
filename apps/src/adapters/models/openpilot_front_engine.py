"""ONNX Runtime adapter for the bounded openpilot front-camera model."""

from __future__ import annotations

import base64
import hashlib
import logging
import pickle
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from adapters.models.openpilot_preprocess import prepare_model_frames, warp_transforms
from domain.front_assistance import (
    FrontCalibration,
    FrontLead,
    FrontPerception,
    FrontReadiness,
)

MODEL_RUN_HZ = 20
FRAME_SKIP = 4
X_IDXS = tuple(192.0 * ((index / 32.0) ** 2) for index in range(33))
EXPECTED_INPUTS = {
    "img": ([1, 12, 128, 256], "tensor(uint8)"),
    "big_img": ([1, 12, 128, 256], "tensor(uint8)"),
    "features_buffer": ([1, 24, 512], "tensor(float16)"),
    "desire_pulse": ([1, 25, 8], "tensor(float16)"),
    "traffic_convention": ([1, 2], "tensor(float16)"),
    "action_t": ([1, 2], "tensor(float16)"),
}
LOG = logging.getLogger("deepstream.front-assistance")


def _safe_exp(value: np.ndarray) -> np.ndarray:
    return np.exp(np.clip(value, -np.inf, 11))


def _sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + _safe_exp(-value))


def _softmax(value: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = value - np.max(value, axis=axis, keepdims=True)
    result = _safe_exp(shifted)
    return result / np.sum(result, axis=axis, keepdims=True)


def _mdn(raw: np.ndarray, shape: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    flattened = raw.reshape(raw.shape[0], -1)
    size = int(np.prod(shape))
    if flattened.shape[1] != size * 2:
        raise ValueError(f"invalid MDN output width {flattened.shape[1]} for {shape}")
    return flattened[:, :size].reshape((raw.shape[0],) + shape), _safe_exp(
        flattened[:, size:]
    ).reshape((raw.shape[0],) + shape)


def parse_model_output(output: np.ndarray, slices: dict[str, slice]) -> dict[str, np.ndarray]:
    if output.shape != (1, 2576) or not np.isfinite(output).all():
        raise ValueError(f"invalid front model output: shape={output.shape}")
    raw = {name: output[:, section] for name, section in slices.items() if name != "pad"}
    plan, plan_stds = _mdn(raw["plan"], (33, 15))
    lanes, lane_stds = _mdn(raw["lane_lines"], (4, 33, 2))
    edges, edge_stds = _mdn(raw["road_edges"], (2, 33, 2))
    leads, lead_stds = _mdn(raw["lead"], (3, 6, 4))
    pose, pose_stds = _mdn(raw["pose"], (6,))
    road_transform, road_transform_stds = _mdn(raw["road_transform"], (6,))
    wide_euler, wide_euler_stds = _mdn(raw["wide_from_device_euler"], (3,))
    return {
        **raw,
        "plan": plan,
        "plan_stds": plan_stds,
        "lane_lines": lanes,
        "lane_lines_stds": lane_stds,
        "lane_lines_prob": _sigmoid(raw["lane_lines_prob"]),
        "road_edges": edges,
        "road_edges_stds": edge_stds,
        "lead": leads,
        "lead_stds": lead_stds,
        "lead_prob": _sigmoid(raw["lead_prob"]),
        "pose": pose,
        "pose_stds": pose_stds,
        "road_transform": road_transform,
        "road_transform_stds": road_transform_stds,
        "wide_from_device_euler": wide_euler,
        "wide_from_device_euler_stds": wide_euler_stds,
        "meta": _sigmoid(raw["meta"]),
        "desire_pred": _softmax(raw["desire_pred"].reshape(1, 4, 8)),
        "desire_state": _softmax(raw["desire_state"].reshape(1, 8)),
    }


class OpenpilotFrontEngine:
    """One ordered recurrent session owned by one front camera worker."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        front = config.get("front_assistance", {}) or {}
        self.enabled = bool(front.get("enabled", False))
        self.interval_seconds = 1.0 / float(front.get("model_rate_hz", MODEL_RUN_HZ))
        self.max_gap_seconds = float(front.get("max_gap_seconds", 0.25))
        self.model_path = Path(str(front.get("model_path", "")))
        self.model_hash = ""
        self.provider = "disabled"
        self.session: Any | None = None
        self.output_slices: dict[str, slice] = {}
        self.calibration = self._load_calibration(front.get("calibration", {}) or {})
        self._narrow_transforms = (
            warp_transforms(self.calibration, big=False)
            if self.calibration.valid
            else None
        )
        self._wide_transforms = (
            warp_transforms(self.calibration, big=True)
            if self.calibration.valid
            else None
        )
        traffic_convention = str(front.get("traffic_convention", "left_hand")).lower()
        if traffic_convention not in {"left_hand", "right_hand"}:
            raise ValueError(
                "front traffic_convention must be left_hand or right_hand"
            )
        self.traffic_convention = traffic_convention
        self._desire_pulse = np.zeros((1, 25, 8), dtype=np.float16)
        self._traffic_convention = np.array(
            [[0.0, 1.0] if traffic_convention == "right_hand" else [1.0, 0.0]],
            dtype=np.float16,
        )
        self._action_t = np.array([[0.075, 0.375]], dtype=np.float16)
        self._epoch: str | None = None
        self._last_timestamp: float | None = None
        self._image_queue: deque[np.ndarray] = deque(maxlen=5)
        self._big_image_queue: deque[np.ndarray] = deque(maxlen=5)
        self._feature_queue: deque[np.ndarray] = deque(maxlen=96)
        self._brake_5: deque[float] = deque(maxlen=5)
        self._brake_3: deque[float] = deque(maxlen=2)
        if not self.enabled:
            return
        if not self.model_path.is_file():
            raise FileNotFoundError(f"front model does not exist: {self.model_path}")
        self.model_hash = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        self._create_session(front, session_factory)
        self.reset("initial")

    @staticmethod
    def _load_calibration(raw: dict[str, Any]) -> FrontCalibration:
        intrinsic = raw.get("intrinsics") or []
        if len(intrinsic) != 3 or any(len(row) != 3 for row in intrinsic):
            intrinsic = [[0.0, 0.0, 0.0]] * 3
        return FrontCalibration(
            profile_id=str(raw.get("profile_id", "unconfigured")),
            source_width=int(raw.get("source_width", 0)),
            source_height=int(raw.get("source_height", 0)),
            intrinsics=tuple(tuple(float(value) for value in row) for row in intrinsic),
            rpy_calib=tuple(float(value) for value in raw.get("rpy_calib", [0.0, 0.0, 0.0])),
            artifact_hash=str(raw.get("artifact_hash", "")),
            valid=bool(raw.get("valid", False)),
        )

    def _create_session(
        self,
        front: dict[str, Any],
        session_factory: Callable[..., Any] | None,
    ) -> None:
        session_options = None
        if session_factory is None:
            import onnxruntime as ort

            session_factory = ort.InferenceSession
            available = set(ort.get_available_providers())
            session_options = ort.SessionOptions()
            session_options.intra_op_num_threads = max(
                1, int(front.get("onnx_intra_op_threads", 1))
            )
            session_options.inter_op_num_threads = max(
                1, int(front.get("onnx_inter_op_threads", 1))
            )
            session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        else:
            available = set(front.get("available_providers", []))
        requested = list(
            front.get(
                "providers",
                ["TensorrtExecutionProvider", "CUDAExecutionProvider"],
            )
        )
        if bool(front.get("allow_cpu", False)):
            requested.append("CPUExecutionProvider")
        providers = list(
            dict.fromkeys(
                provider
                for provider in requested
                if not available or provider in available
            )
        )
        if not providers:
            raise RuntimeError("front model has no permitted ONNX Runtime provider")
        provider_errors: dict[str, str] = {}
        for provider in providers:
            try:
                kwargs: dict[str, Any] = {"providers": [provider]}
                if session_options is not None:
                    kwargs["sess_options"] = session_options
                self.session = session_factory(str(self.model_path), **kwargs)
                break
            except Exception as exc:
                provider_errors[provider] = str(exc)
                LOG.warning(
                    "front provider initialization failed: provider=%s error=%s",
                    provider,
                    exc,
                )
        if self.session is None:
            raise RuntimeError(
                f"front model providers failed to initialize: {provider_errors}"
            )
        self.provider = str(self.session.get_providers()[0])
        actual = {item.name: (list(item.shape), str(item.type)) for item in self.session.get_inputs()}
        if actual != EXPECTED_INPUTS:
            raise ValueError(f"front model input contract mismatch: {actual}")
        outputs = self.session.get_outputs()
        if len(outputs) != 1 or list(outputs[0].shape) != [1, 2576]:
            raise ValueError("front model output contract mismatch")
        metadata = self.session.get_modelmeta().custom_metadata_map
        encoded = metadata.get("output_slices")
        if not encoded:
            raise ValueError("front model output_slices metadata is missing")
        decoded = pickle.loads(base64.b64decode(encoded))  # noqa: S301 - trusted pinned model
        self.output_slices = {str(name): section for name, section in decoded.items()}

    def reset(self, source_epoch: str) -> None:
        self._epoch = source_epoch
        self._last_timestamp = None
        zero_image = np.zeros((6, 128, 256), dtype=np.uint8)
        self._image_queue = deque((zero_image.copy() for _ in range(5)), maxlen=5)
        self._big_image_queue = deque((zero_image.copy() for _ in range(5)), maxlen=5)
        self._feature_queue = deque(
            (np.zeros(512, dtype=np.float16) for _ in range(96)), maxlen=96
        )
        self._brake_5.clear()
        self._brake_3.clear()

    def process(
        self,
        frame: np.ndarray,
        *,
        source_epoch: str,
        frame_number: int,
        source_timestamp: float,
    ) -> FrontPerception:
        if self.session is None:
            raise RuntimeError("front model is disabled")
        if (
            source_epoch != self._epoch
            or self._last_timestamp is None
            or source_timestamp <= self._last_timestamp
            or source_timestamp - self._last_timestamp > self.max_gap_seconds
        ):
            self.reset(source_epoch)
        self._last_timestamp = source_timestamp
        blocking: list[str] = []
        if not self.calibration.valid:
            blocking.append("calibration_invalid")
        if blocking:
            return self._empty_perception(
                source_epoch, frame_number, source_timestamp, tuple(blocking)
            )

        narrow, wide = prepare_model_frames(
            frame,
            self.calibration,
            narrow_transforms=self._narrow_transforms,
            wide_transforms=self._wide_transforms,
        )
        self._image_queue.append(narrow)
        self._big_image_queue.append(wide)
        img = np.concatenate((self._image_queue[0], self._image_queue[4]), axis=0)[None]
        big_img = np.concatenate(
            (self._big_image_queue[0], self._big_image_queue[4]), axis=0
        )[None]
        features = np.stack(tuple(self._feature_queue)[::FRAME_SKIP], axis=0)[None]
        inputs = {
            "img": img,
            "big_img": big_img,
            "features_buffer": features.astype(np.float16, copy=False),
            "desire_pulse": self._desire_pulse,
            "traffic_convention": self._traffic_convention,
            "action_t": self._action_t,
        }
        started = time.perf_counter()
        raw = np.asarray(self.session.run(None, inputs)[0])
        inference_ms = (time.perf_counter() - started) * 1000.0
        parsed = parse_model_output(raw, self.output_slices)
        hidden = raw[0, self.output_slices["hidden_state"]].astype(np.float16)
        self._feature_queue.append(hidden)
        meta = parsed["meta"][0]
        self._brake_5.append(float(meta[6]))
        self._brake_3.append(float(meta[4]))
        hard_brake = (
            len(self._brake_5) == 5
            and len(self._brake_3) == 2
            and all(
                value > threshold
                for value, threshold in zip(
                    self._brake_5, (0.05, 0.05, 0.15, 0.15, 0.15), strict=True
                )
            )
            and all(value > 0.7 for value in self._brake_3)
        )
        lane_values = parsed["lane_lines"][0]
        edge_values = parsed["road_edges"][0]
        plan = parsed["plan"][0]
        lead_values = parsed["lead"][0]
        lead_probs = parsed["lead_prob"][0]
        lane_probabilities = parsed["lane_lines_prob"][0, 1::2]
        desire = parsed["desire_pred"][0, 0]
        return FrontPerception(
            source_epoch=source_epoch,
            frame_number=frame_number,
            source_timestamp=source_timestamp,
            valid=True,
            readiness=FrontReadiness.READY,
            blocking_reasons=(),
            lane_lines=tuple(
                tuple(
                    (X_IDXS[index], float(point[0]), float(point[1]))
                    for index, point in enumerate(line)
                )
                for line in lane_values
            ),
            lane_probabilities=tuple(float(value) for value in lane_probabilities),
            road_edges=tuple(
                tuple(
                    (X_IDXS[index], float(point[0]), float(point[1]))
                    for index, point in enumerate(edge)
                )
                for edge in edge_values
            ),
            path=tuple(
                (float(point[0]), float(point[1]), float(point[2])) for point in plan
            ),
            leads=tuple(
                FrontLead(
                    probability=float(lead_probs[index]),
                    x=tuple(float(value) for value in lead[:, 0]),
                    y=tuple(float(value) for value in lead[:, 1]),
                    velocity=tuple(float(value) for value in lead[:, 2]),
                    acceleration=tuple(float(value) for value in lead[:, 3]),
                )
                for index, lead in enumerate(lead_values)
            ),
            desire_prediction=tuple(float(value) for value in desire),
            hard_brake_predicted=hard_brake,
            hard_brake_3_probs=tuple(self._brake_3),
            hard_brake_5_probs=tuple(self._brake_5),
            provider=self.provider,
            inference_ms=inference_ms,
            model_hash=self.model_hash,
            calibration_hash=self.calibration.artifact_hash,
            diagnostics={
                "model_rate_hz": round(1.0 / self.interval_seconds, 3),
                "path_x_range": [
                    round(float(np.min(plan[:, 0])), 4),
                    round(float(np.max(plan[:, 0])), 4),
                ],
                "path_y_range": [
                    round(float(np.min(plan[:, 1])), 4),
                    round(float(np.max(plan[:, 1])), 4),
                ],
                "path_z_range": [
                    round(float(np.min(plan[:, 2])), 4),
                    round(float(np.max(plan[:, 2])), 4),
                ],
            },
        )

    def _empty_perception(
        self,
        source_epoch: str,
        frame_number: int,
        source_timestamp: float,
        blocking_reasons: tuple[str, ...],
    ) -> FrontPerception:
        return FrontPerception(
            source_epoch,
            frame_number,
            source_timestamp,
            False,
            FrontReadiness.NOT_READY,
            blocking_reasons,
            (),
            (),
            (),
            (),
            (),
            (),
            False,
            (),
            (),
            self.provider,
            0.0,
            self.model_hash,
            self.calibration.artifact_hash,
        )
