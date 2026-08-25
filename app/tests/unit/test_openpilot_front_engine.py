from __future__ import annotations

import base64
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from adapters.models.openpilot_front_engine import (
    EXPECTED_INPUTS,
    OpenpilotFrontEngine,
    parse_model_output,
)
from adapters.models.openpilot_preprocess import (
    MEDMODEL_INTRINSICS,
    VIEW_FROM_DEVICE,
    _rotation_from_euler,
    prepare_model_frame,
    warp_matrix,
)
from domain.front_assistance import FrontCalibration

OUTPUT_SLICES = {
    "meta": slice(0, 55),
    "desire_pred": slice(55, 87),
    "pose": slice(87, 99),
    "wide_from_device_euler": slice(99, 105),
    "road_transform": slice(105, 117),
    "lane_lines": slice(117, 645),
    "lane_lines_prob": slice(645, 653),
    "road_edges": slice(653, 917),
    "lead": slice(917, 1061),
    "lead_prob": slice(1061, 1064),
    "hidden_state": slice(1064, 1576),
    "plan": slice(1576, 2566),
    "desire_state": slice(2566, 2574),
    "pad": slice(-2, None),
}


@dataclass
class _Node:
    name: str
    shape: list[int]
    type: str


class _Meta:
    custom_metadata_map = {
        "output_slices": base64.b64encode(pickle.dumps(OUTPUT_SLICES)).decode()
    }


class _Session:
    def __init__(self, _path: str, providers: list[str]) -> None:
        self.providers = providers
        self.inputs: dict[str, np.ndarray] | None = None

    def get_inputs(self) -> list[_Node]:
        return [_Node(name, shape, dtype) for name, (shape, dtype) in EXPECTED_INPUTS.items()]

    def get_outputs(self) -> list[_Node]:
        return [_Node("outputs", [1, 2576], "tensor(float16)")]

    def get_providers(self) -> list[str]:
        return self.providers

    def get_modelmeta(self) -> _Meta:
        return _Meta()

    def run(self, _names: Any, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.inputs = inputs
        return [np.zeros((1, 2576), dtype=np.float16)]


def _config(model: Path) -> dict[str, Any]:
    return {
        "front_assistance": {
            "enabled": True,
            "model_path": str(model),
            "providers": ["CUDAExecutionProvider"],
            "available_providers": ["CUDAExecutionProvider"],
            "calibration": {
                "profile_id": "fixture",
                "source_width": 8,
                "source_height": 8,
                "intrinsics": [[6.0, 0.0, 4.0], [0.0, 6.0, 4.0], [0.0, 0.0, 1.0]],
                "rpy_calib": [0.0, 0.0, 0.0],
                "artifact_hash": "calibration-sha",
                "valid": True,
            },
        }
    }


def test_preprocess_produces_openpilot_tensor_shape() -> None:
    calibration = FrontCalibration(
        "fixture",
        8,
        8,
        ((6.0, 0.0, 4.0), (0.0, 6.0, 4.0), (0.0, 0.0, 1.0)),
        valid=True,
    )
    tensor = prepare_model_frame(np.zeros((8, 8, 3), dtype=np.uint8), calibration, big=False)
    assert tensor.shape == (6, 128, 256)
    assert tensor.dtype == np.uint8


def test_warp_matrix_matches_openpilot_reference_order() -> None:
    calibration = FrontCalibration(
        "fixture",
        960,
        540,
        (
            (759.85, 0.0, 489.76),
            (0.0, 759.85, 294.90),
            (0.0, 0.0, 1.0),
        ),
        rpy_calib=(0.01, -0.02, 0.03),
        valid=True,
    )
    camera_intrinsics = np.asarray(calibration.intrinsics, dtype=np.float32)
    expected = (
        camera_intrinsics
        @ VIEW_FROM_DEVICE
        @ _rotation_from_euler(calibration.rpy_calib)
        @ np.linalg.inv(MEDMODEL_INTRINSICS @ VIEW_FROM_DEVICE)
    )

    assert np.allclose(warp_matrix(calibration, big=False), expected, atol=1e-5)


def test_output_parser_shapes_and_probabilities() -> None:
    parsed = parse_model_output(np.zeros((1, 2576), dtype=np.float16), OUTPUT_SLICES)
    assert parsed["plan"].shape == (1, 33, 15)
    assert parsed["lane_lines"].shape == (1, 4, 33, 2)
    assert parsed["road_edges"].shape == (1, 2, 33, 2)
    assert parsed["lead"].shape == (1, 3, 6, 4)
    assert np.allclose(parsed["lane_lines_prob"], 0.5)
    assert np.allclose(parsed["desire_state"].sum(axis=-1), 1.0)


def test_engine_uses_single_frame_for_both_contexts_and_resets_on_epoch(tmp_path: Path) -> None:
    model = tmp_path / "driving_supercombo.onnx"
    model.write_bytes(b"pinned-model")
    session = _Session("", ["CUDAExecutionProvider"])
    engine = OpenpilotFrontEngine(_config(model), session_factory=lambda *_args, **_kwargs: session)
    frame = np.zeros((8, 8, 3), dtype=np.uint8)

    result = engine.process(
        frame,
        source_epoch="epoch-1",
        frame_number=1,
        source_timestamp=1.0,
    )

    assert result.valid is True
    assert result.provider == "CUDAExecutionProvider"
    assert session.inputs is not None
    assert session.inputs["img"].shape == (1, 12, 128, 256)
    assert session.inputs["big_img"].shape == (1, 12, 128, 256)
    assert session.inputs["features_buffer"].shape == (1, 24, 512)
    assert session.inputs["traffic_convention"].tolist() == [[1.0, 0.0]]
    assert len(result.lane_lines[0][0]) == 3
    engine.process(frame, source_epoch="epoch-2", frame_number=2, source_timestamp=2.0)
    assert engine._epoch == "epoch-2"


def test_engine_supports_right_hand_traffic_convention(tmp_path: Path) -> None:
    model = tmp_path / "driving_supercombo.onnx"
    model.write_bytes(b"pinned-model")
    config = _config(model)
    config["front_assistance"]["traffic_convention"] = "right_hand"
    session = _Session("", ["CUDAExecutionProvider"])
    engine = OpenpilotFrontEngine(
        config, session_factory=lambda *_args, **_kwargs: session
    )

    engine.process(
        np.zeros((8, 8, 3), dtype=np.uint8),
        source_epoch="epoch",
        frame_number=1,
        source_timestamp=1.0,
    )

    assert session.inputs is not None
    assert session.inputs["traffic_convention"].tolist() == [[0.0, 1.0]]


def test_invalid_calibration_fails_closed_without_inference(tmp_path: Path) -> None:
    model = tmp_path / "driving_supercombo.onnx"
    model.write_bytes(b"pinned-model")
    config = _config(model)
    config["front_assistance"]["calibration"]["valid"] = False
    session = _Session("", ["CUDAExecutionProvider"])
    engine = OpenpilotFrontEngine(config, session_factory=lambda *_args, **_kwargs: session)

    result = engine.process(
        np.zeros((8, 8, 3), dtype=np.uint8),
        source_epoch="epoch",
        frame_number=1,
        source_timestamp=1.0,
    )

    assert result.valid is False
    assert result.blocking_reasons == ("calibration_invalid",)
    assert session.inputs is None


def test_engine_falls_back_from_tensorrt_to_cuda(tmp_path: Path) -> None:
    model = tmp_path / "driving_supercombo.onnx"
    model.write_bytes(b"pinned-model")
    config = _config(model)
    config["front_assistance"]["providers"] = [
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
    ]
    config["front_assistance"]["available_providers"] = list(
        config["front_assistance"]["providers"]
    )

    def factory(_path: str, providers: list[str]) -> _Session:
        if providers == ["TensorrtExecutionProvider"]:
            raise RuntimeError("unsupported TensorRT kernel")
        return _Session("", providers)

    engine = OpenpilotFrontEngine(config, session_factory=factory)
    assert engine.provider == "CUDAExecutionProvider"
