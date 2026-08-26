"""Explicit function capabilities used to compile camera execution plans."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

IntervalResolver = Callable[[dict[str, Any]], float]
ModelResolver = Callable[[dict[str, Any]], tuple[str, ...]]


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    return config.get(name, {}) or {}


def _face_interval(config: dict[str, Any]) -> float:
    face = _section(config, "recognition").get("face_runtime", {}) or {}
    return max(300.0, min(500.0, float(face.get("recognition_interval_ms", 400)))) / 1000.0


def _dms_interval(config: dict[str, Any]) -> float:
    dms = _section(config, "dms")
    behavior = max(0.1, float(dms.get("interval_ms", 500)) / 1000.0)
    attention = dms.get("attention", {}) or {}
    attention_rate = max(0.05, float(attention.get("interval_ms", 100)) / 1000.0)
    return min(behavior, attention_rate)


def _clamped_interval(
    section_name: str, minimum: float, maximum: float | None = None
) -> IntervalResolver:
    def resolve(config: dict[str, Any]) -> float:
        interval = float(_section(config, section_name).get("interval_ms", 500)) / 1000.0
        bounded = max(minimum, interval)
        return min(maximum, bounded) if maximum is not None else bounded

    return resolve


def _front_interval(config: dict[str, Any]) -> float:
    rate_hz = max(0.001, float(_section(config, "front_assistance").get("model_rate_hz", 20)))
    return 1.0 / rate_hz


def _face_models(config: dict[str, Any]) -> tuple[str, ...]:
    face = _section(config, "recognition").get("face_runtime", {}) or {}
    return tuple(
        str(value)
        for value in (face.get("detector_model"), face.get("recognizer_model"))
        if value
    )


def _single_model(section_name: str, key: str = "onnx_path") -> ModelResolver:
    def resolve(config: dict[str, Any]) -> tuple[str, ...]:
        value = str(_section(config, section_name).get(key, "")).strip()
        return (value,) if value else ()

    return resolve


def _object_models(section: dict[str, Any]) -> tuple[str, ...]:
    models = (section.get("object_detection", {}) or {}).get("models", {}) or {}
    return tuple(
        str(model.get("onnx_path"))
        for model in models.values()
        if isinstance(model, dict) and model.get("onnx_path")
    )


def _dms_models(config: dict[str, Any]) -> tuple[str, ...]:
    return _object_models(_section(config, "dms"))


def _smoking_models(config: dict[str, Any]) -> tuple[str, ...]:
    section = _section(config, "smoking_behavior")
    primary = str(section.get("onnx_path", "")).strip()
    return ((primary,) if primary else ()) + _object_models(section)


@dataclass(frozen=True, slots=True)
class FunctionRegistration:
    name: str
    config_section: str
    shared_nodes: tuple[str, ...]
    processor_method: str
    engine_attribute: str
    engine_module: str
    engine_class: str
    interval_resolver: IntervalResolver
    model_resolver: ModelResolver
    passes_trace_sink: bool = False


FUNCTION_REGISTRY: dict[str, FunctionRegistration] = {
    "face_recognition": FunctionRegistration(
        "face_recognition",
        "recognition",
        ("person_inference", "cpu_frame"),
        "_process_face_sample",
        "face_engine",
        "adapters.models.face_engine",
        "FaceRecognitionEngine",
        _face_interval,
        _face_models,
        True,
    ),
    "dms": FunctionRegistration(
        "dms",
        "dms",
        ("person_inference", "cpu_frame"),
        "_process_dms_sample",
        "dms_engine",
        "adapters.models.dms_engine",
        "DmsBehaviorEngine",
        _dms_interval,
        _dms_models,
    ),
    "smoking_behavior": FunctionRegistration(
        "smoking_behavior",
        "smoking_behavior",
        ("person_inference", "cpu_frame"),
        "_process_smoking_sample",
        "smoking_behavior_engine",
        "adapters.models.smoking_engine",
        "SmokingBehaviorEngine",
        _clamped_interval("smoking_behavior", 0.3, 0.5),
        _smoking_models,
    ),
    "fire_smoke": FunctionRegistration(
        "fire_smoke",
        "fire_smoke",
        ("person_inference", "cpu_frame"),
        "_process_fire_smoke_sample",
        "fire_smoke_engine",
        "adapters.models.fire_smoke_engine",
        "FireSmokeEngine",
        _clamped_interval("fire_smoke", 0.2),
        _single_model("fire_smoke"),
    ),
    "front_assistance": FunctionRegistration(
        "front_assistance",
        "front_assistance",
        ("cpu_frame",),
        "_process_front_sample",
        "front_engine",
        "adapters.models.openpilot_front_engine",
        "OpenpilotFrontEngine",
        _front_interval,
        _single_model("front_assistance", "model_path"),
    ),
}


def registration_for(name: str) -> FunctionRegistration:
    try:
        return FUNCTION_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unregistered camera function: {name}") from exc
