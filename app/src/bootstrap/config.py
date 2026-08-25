"""Configuration loading and per-camera normalization for DeepStream Safety."""

from __future__ import annotations

import math
import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml


def _load_raw_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError("DeepStream config root must be a mapping")
    extends = data.pop("extends", None)
    if extends:
        base_path = (path.parent / str(extends)).resolve()
        if base_path == path.resolve():
            raise ValueError("config cannot extend itself")
        data = _merge_config(_load_raw_config(base_path), data)
    return data


def _apply_runtime_environment_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Apply process-owned runtime directories after all config inheritance."""
    environment = _runtime_environment()
    runtime = config.setdefault("runtime", {})
    evidence = config.setdefault("evidence", {})
    overrides = {
        "CAMERA_STATUS_DIR": (runtime, "status_directory"),
        "CAMERA_STATE_DIR": (runtime, "state_directory"),
        "CAMERA_EVIDENCE_DIR": (evidence, "directory"),
    }
    for environment_key, (section, config_key) in overrides.items():
        value = environment.get(environment_key)
        if value:
            section[config_key] = value
    return config


def load_raw_config(path: Path) -> dict[str, Any]:
    """Load inherited YAML and then apply runtime-only environment overrides."""
    return _apply_runtime_environment_overrides(_load_raw_config(path))


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def validate_config(config: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Fail closed on deployment errors before any worker or model is started."""
    profile = str(config.get("profile", "dev")).lower()
    runtime_config = config.get("runtime", {}) or {}
    if float(runtime_config.get("live_metadata_interval_ms", 250)) < 100.0:
        raise ValueError("runtime.live_metadata_interval_ms must be at least 100")
    person_config = config.get("person", {}) or {}
    person_inference_interval = int(person_config.get("inference_interval", 0))
    if not 0 <= person_inference_interval <= 30:
        raise ValueError("person.inference_interval must be in [0, 30]")
    if float(person_config.get("roi_cache_max_age_seconds", 0.5)) <= 0.0:
        raise ValueError("person.roi_cache_max_age_seconds must be positive")
    ids = camera_ids(config)
    if not ids:
        raise ValueError("at least one camera is required")
    sync_groups: dict[str, tuple[float, float]] = {}
    for camera in config.get("cameras", []) or []:
        camera_id = str(camera["id"])
        source = camera.get("source", {}) or {}
        source_type = str(source.get("type", source.get("mode", "rtsp"))).lower()
        media_only = bool(source.get("media_only", False))
        if media_only and source_type != "mock":
            raise ValueError(f"camera {camera_id} media_only requires a mock source")
        sync_group = str(source.get("sync_group", "")).strip()
        sync_period = float(source.get("sync_period_seconds", 0.0))
        sync_epoch = float(source.get("sync_epoch_seconds", 0.0))
        if sync_group or sync_period != 0.0:
            if source_type != "mock":
                raise ValueError(f"camera {camera_id} synchronized source must be mock")
            if (
                not sync_group
                or not math.isfinite(sync_period)
                or sync_period <= 0.0
                or not math.isfinite(sync_epoch)
            ):
                raise ValueError(
                    f"camera {camera_id} synchronized mock requires sync_group, "
                    "positive sync_period_seconds and finite sync_epoch_seconds"
                )
            group_contract = (sync_period, sync_epoch)
            existing_contract = sync_groups.setdefault(sync_group, group_contract)
            if existing_contract != group_contract:
                raise ValueError(
                    f"camera {camera_id} sync timeline differs from group {sync_group}"
                )
        if media_only:
            enabled_functions = [
                name
                for name, enabled in (camera.get("functions", {}) or {}).items()
                if bool(enabled)
            ]
            if enabled_functions:
                raise ValueError(
                    f"camera {camera_id} media_only cannot enable functions: "
                    f"{', '.join(sorted(enabled_functions))}"
                )
            if not sync_group or sync_period <= 0.0:
                raise ValueError(
                    f"camera {camera_id} media_only requires sync_group and positive sync_period_seconds"
                )
        source_url = str(source.get("url", source.get("rtsp_url", "")))
        output_url = str((camera.get("output", {}) or {}).get("rtsp_url", ""))
        for label, value in (("source", source_url), ("output", output_url)):
            parsed = urlsplit(value)
            if parsed.scheme not in {"rtsp", "rtsps", "rtmp"} or not parsed.netloc:
                raise ValueError(f"camera {camera_id} {label} URL is invalid: {value}")
        functions = camera.get("functions", {}) or {}
        if bool(functions.get("front_assistance", False)):
            if media_only:
                raise ValueError(
                    f"camera {camera_id} front_assistance cannot use media_only"
                )
            if source_url == output_url:
                raise ValueError(
                    f"camera {camera_id} front_assistance source and output URLs must differ"
                )
            front = _merge_config(
                config.get("front_assistance", {}) or {},
                camera.get("front_assistance", {}) or {},
            )
            calibration = front.get("calibration", {}) or {}
            intrinsic = calibration.get("intrinsics", [])
            rpy_calib = calibration.get("rpy_calib", [])
            traffic_convention = str(
                front.get("traffic_convention", "left_hand")
            ).lower()
            if (
                int(front.get("model_rate_hz", 20)) != 20
                or int(calibration.get("source_width", 0)) <= 0
                or int(calibration.get("source_height", 0)) <= 0
                or len(intrinsic) != 3
                or any(not isinstance(row, list) or len(row) != 3 for row in intrinsic)
                or len(rpy_calib) != 3
                or traffic_convention not in {"left_hand", "right_hand"}
            ):
                raise ValueError(
                    f"camera {camera_id} front_assistance calibration/model rate is invalid"
                )

    cameras = config.get("cameras", []) or []
    all_mock_sources = bool(cameras) and all(
        str((camera.get("source", {}) or {}).get("type", "rtsp")).lower() == "mock"
        for camera in cameras
        if isinstance(camera, dict)
    )
    validate_models = (
        profile == "production"
        or (profile == "e2e" and not all_mock_sources)
        or os.environ.get("CAMERA_VALIDATE_MODELS") == "1"
    )
    model_paths = [
        (config.get("person", {}) or {}).get("onnx_path"),
        (config.get("person", {}) or {}).get("engine_path"),
        ((config.get("recognition", {}) or {}).get("face_runtime", {}) or {}).get("detector_model"),
        ((config.get("recognition", {}) or {}).get("face_runtime", {}) or {}).get("recognizer_model"),
        (config.get("smoking_behavior", {}) or {}).get("onnx_path"),
        (config.get("fire_smoke", {}) or {}).get("onnx_path"),
    ]
    dms_config = config.get("dms", {}) or {}
    dms_object = dms_config.get("object_detection", {}) or {}
    if dms_object.get("enabled", False):
        model_paths.extend(
            (model or {}).get("onnx_path")
            for model in (dms_object.get("models", {}) or {}).values()
        )
    for camera in config.get("cameras", []) or []:
        camera_fire_smoke = (camera.get("fire_smoke", {}) or {}) if isinstance(camera, dict) else {}
        camera_analysis = (camera.get("analysis", {}) or {}) if isinstance(camera, dict) else {}
        camera_fire_override = (camera_analysis.get("functions", {}) or {}).get("fire_smoke", {}) or {}
        model_paths.extend(
            [camera_fire_smoke.get("onnx_path"), camera_fire_override.get("onnx_path"), camera_fire_override.get("model_path")]
        )
        if bool((camera.get("functions", {}) or {}).get("front_assistance", False)):
            camera_front = _merge_config(
                config.get("front_assistance", {}) or {},
                camera.get("front_assistance", {}) or {},
            )
            model_paths.append(camera_front.get("model_path"))
    smoking_object = (
        (config.get("smoking_behavior", {}) or {}).get("object_detection", {}) or {}
    )
    if smoking_object.get("enabled", False):
        model_paths.extend(
            (model or {}).get("onnx_path")
            for model in (smoking_object.get("models", {}) or {}).values()
        )
    if dms_object.get("enabled", False):
        dms_models = dms_object.get("models", {}) or {}
        if not isinstance(dms_models, dict) or not dms_models:
            raise ValueError("dms.object_detection.models must be a non-empty mapping")
        confidence = float(dms_object.get("confidence", 0.35))
        nms_iou = float(dms_object.get("nms_iou", 0.50))
        person_match_iou = float(dms_object.get("person_match_iou", 0.10))
        if not 0.0 < confidence <= 1.0:
            raise ValueError("dms.object_detection.confidence must be in (0, 1]")
        if not 0.0 < nms_iou <= 1.0 or not 0.0 <= person_match_iou <= 1.0:
            raise ValueError("dms object detection IoU values are invalid")
        for source, model in dms_models.items():
            labels = [str(label) for label in (model or {}).get("labels", ())]
            positive_labels = [
                str(label) for label in (model or {}).get("positive_labels", ())
            ]
            if not labels or not positive_labels or not set(positive_labels).issubset(labels):
                raise ValueError(f"dms object model {source} has invalid labels")
        event_policy = dms_config.get("event_policy", {}) or {}
        for section_name in ("attention", "model", "face", "no_seatbelt"):
            section = event_policy.get(section_name, {}) or {}
            hits = int(section.get("confirmation_hits", 1))
            window = int(section.get("confirmation_window", hits))
            if hits < 1 or window < hits:
                raise ValueError(
                    f"dms.event_policy.{section_name} confirmation window is invalid"
                )
            for key in (
                "minimum_duration_seconds",
                "candidate_timeout_seconds",
                "clear_seconds",
                "unknown_timeout_seconds",
            ):
                if float(section.get(key, 0.0)) < 0.0:
                    raise ValueError(
                        f"dms.event_policy.{section_name}.{key} cannot be negative"
                    )
            if float(section.get("trace_interval_ms", 1.0)) <= 0.0:
                raise ValueError(
                    f"dms.event_policy.{section_name}.trace_interval_ms must be positive"
                )
        model_policy = event_policy.get("model", {}) or {}
        minimum_event_score = float(model_policy.get("min_score", confidence))
        if not confidence <= minimum_event_score <= 1.0:
            raise ValueError(
                "dms.event_policy.model.min_score must be between detector confidence and 1"
            )
        face_policy = dms_config.get("face_mesh", {}) or {}
        for key in ("ear_threshold", "mar_threshold"):
            if not 0.0 < float(face_policy.get(key, 0.5)) < 1.0:
                raise ValueError(f"dms.face_mesh.{key} must be in (0, 1)")
        for key in ("yaw_threshold_deg", "pitch_threshold_deg"):
            if float(face_policy.get(key, 1.0)) <= 0.0:
                raise ValueError(f"dms.face_mesh.{key} must be positive")
        driver_roi = face_policy.get("driver_roi", {}) or {}
        if not 0.2 <= float(driver_roi.get("upper_body_ratio", 0.55)) <= 1.0:
            raise ValueError(
                "dms.face_mesh.driver_roi.upper_body_ratio must be in [0.2, 1]"
            )
        if not 0.0 <= float(driver_roi.get("padding_ratio", 0.10)) <= 0.5:
            raise ValueError(
                "dms.face_mesh.driver_roi.padding_ratio must be in [0, 0.5]"
            )
        if int(driver_roi.get("max_side", 640)) < 256:
            raise ValueError("dms.face_mesh.driver_roi.max_side must be at least 256")
        calibration = face_policy.get("neutral_calibration", {}) or {}
        minimum_samples = int(calibration.get("minimum_samples", 15))
        window_size = int(calibration.get("window_size", 30))
        if minimum_samples < 3 or window_size < minimum_samples:
            raise ValueError(
                "dms.face_mesh.neutral_calibration sample window is invalid"
            )
        for key in (
            "max_yaw_std_deg",
            "max_pitch_std_deg",
            "neutral_update_max_delta_deg",
        ):
            if float(calibration.get(key, 1.0)) <= 0.0:
                raise ValueError(
                    f"dms.face_mesh.neutral_calibration.{key} must be positive"
                )
        update_alpha = float(calibration.get("neutral_update_alpha", 0.01))
        if not 0.0 <= update_alpha <= 1.0:
            raise ValueError(
                "dms.face_mesh.neutral_calibration.neutral_update_alpha "
                "must be in [0, 1]"
            )
        attention = dms_config.get("attention", {}) or {}
        if float(attention.get("interval_ms", 100)) < 50.0:
            raise ValueError("dms.attention.interval_ms must be at least 50")
        alert_seconds = [float(item) for item in attention.get("alert_seconds", ())]
        if len(alert_seconds) != 3 or not (
            0.0 < alert_seconds[0] < alert_seconds[1] < alert_seconds[2]
        ):
            raise ValueError("dms.attention.alert_seconds must contain three increasing values")
    allow_engine_build = bool((config.get("runtime", {}) or {}).get("allow_engine_build", False))
    engine_path = str((config.get("person", {}) or {}).get("engine_path", ""))
    if validate_models:
        missing = []
        for item in model_paths:
            if not item or Path(str(item)).is_file():
                continue
            if allow_engine_build and str(item) == engine_path:
                continue
            missing.append(str(item))
        if missing:
            raise ValueError(f"configured model files do not exist: {', '.join(missing)}")
    for section, key in (("smoking_behavior", "providers"), ("fire_smoke", "providers")):
        section_config = config.get(section, {}) or {}
        if section_config.get("require_gpu_provider") and "CUDAExecutionProvider" not in section_config.get(key, []):
            raise ValueError(f"{section} requires CUDAExecutionProvider")

    evidence_dir = Path(str((config.get("evidence", {}) or {}).get("directory", ".tmp/deepstream-safety")))
    state_dir = Path(str((config.get("runtime", {}) or {}).get("state_directory", evidence_dir / "state")))
    if profile == "production":
        for directory in (evidence_dir, state_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if not os.access(directory, os.W_OK):
                raise ValueError(f"runtime path is not writable: {directory}")
    if path is not None:
        config.setdefault("runtime", {})["config_path"] = str(path.resolve())
    return config


def camera_ids(config: dict[str, Any]) -> list[str]:
    cameras = config.get("cameras")
    if cameras is None:
        input_config = config.get("input", {}) or {}
        return [str(input_config.get("camera", "safety_camera"))]
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("cameras must be a non-empty list")
    ids: list[str] = []
    for camera in cameras:
        if not isinstance(camera, dict) or not camera.get("id"):
            raise ValueError("each camera must define a non-empty id")
        camera_id = str(camera["id"])
        if camera_id in ids:
            raise ValueError(f"duplicate camera id: {camera_id}")
        ids.append(camera_id)
    return ids


def _camera_by_id(config: dict[str, Any], camera_id: str) -> dict[str, Any]:
    for camera in config.get("cameras", []) or []:
        if str(camera.get("id")) == camera_id:
            return camera
    raise ValueError(f"camera is not configured: {camera_id}")


def _dotenv_values(path: Path) -> dict[str, str]:
    """Read the small local env file used by the Windows/WSL launcher."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _runtime_environment() -> dict[str, str]:
    env_file = os.environ.get("CAMERA_ENV_FILE")
    values = _dotenv_values(Path(env_file)) if env_file else {}
    values.update({key: value for key, value in os.environ.items() if value})
    return values


def _environment_bool(environment: dict[str, str], key: str) -> bool | None:
    value = environment.get(key)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean value")


def _camera_metadata_url(config: dict[str, Any], camera_id: str, index: int) -> str | None:
    metadata = deepcopy(config.get("metadata", {}) or {})
    camera_metadata = (
        _camera_by_id(config, camera_id).get("metadata", {}) or {}
        if config.get("cameras")
        else {}
    )
    metadata.update(camera_metadata)
    environment_base = _runtime_environment().get("CAMERA_METADATA_ZMQ_BASE", "").strip()
    url = environment_base or metadata.get("zmq_pub_url")
    if not url:
        url = "tcp://127.0.0.1:5555"
    if not url or (camera_metadata.get("zmq_pub_url") and not environment_base):
        return url
    parsed = urlsplit(str(url))
    if parsed.port is None:
        return str(url)
    return urlunsplit(
        (parsed.scheme, f"{parsed.hostname}:{parsed.port + index}", parsed.path, parsed.query, parsed.fragment)
    )


def _normalize_camera_analysis(
    resolved: dict[str, Any], camera: dict[str, Any], camera_id: str
) -> None:
    """Apply the public per-camera analysis schema to legacy model sections."""
    analysis = _merge_config(
        deepcopy(resolved.get("analysis", {}) or {}),
        deepcopy(camera.get("analysis", {}) or {}),
    )
    max_age_ms = int(analysis.get("result_max_age_ms", 2000))
    if max_age_ms < 100:
        raise ValueError(f"camera {camera_id} analysis.result_max_age_ms must be at least 100")
    analysis["result_max_age_ms"] = max_age_ms
    resolved["analysis"] = analysis
    resolved.setdefault("runtime", {})["analysis_result_max_age_seconds"] = (
        max_age_ms / 1000.0
    )

    functions = analysis.get("functions", {}) or {}
    if not isinstance(functions, dict):
        raise ValueError(f"camera {camera_id} analysis.functions must be a mapping")

    fire_override = functions.get("fire_smoke", {}) or {}
    if not isinstance(fire_override, dict):
        raise ValueError(
            f"camera {camera_id} analysis.functions.fire_smoke must be a mapping"
        )
    fire_smoke = deepcopy(resolved.get("fire_smoke", {}) or {})
    # A canary may replace the model for one camera while other workers keep
    # the production baseline. This is configuration-only and does not alter
    # the event/API contract.
    camera_fire_smoke = camera.get("fire_smoke", {}) or {}
    if isinstance(camera_fire_smoke, dict) and camera_fire_smoke.get("onnx_path"):
        fire_smoke["onnx_path"] = str(camera_fire_smoke["onnx_path"])
    for model_key in ("onnx_path", "model_path"):
        if fire_override.get(model_key):
            fire_smoke["onnx_path"] = str(fire_override[model_key])
    if "interval_ms" in fire_override:
        fire_smoke["interval_ms"] = int(fire_override["interval_ms"])
    thresholds = fire_override.get("thresholds", {}) or {}
    if not isinstance(thresholds, dict):
        raise ValueError(f"camera {camera_id} fire_smoke.thresholds must be a mapping")
    for label in ("fire", "smoke"):
        if label in thresholds:
            fire_smoke[f"{label}_threshold"] = float(thresholds[label])
    if "rois" in fire_override:
        rois = fire_override.get("rois") or {}
        if not isinstance(rois, dict):
            raise ValueError(f"camera {camera_id} fire_smoke.rois must be a mapping")
        fire_smoke["class_rois"] = deepcopy(rois)
    for nested_name in ("tracking", "dynamics"):
        nested_override = fire_override.get(nested_name, {}) or {}
        if not isinstance(nested_override, dict):
            raise ValueError(
                f"camera {camera_id} fire_smoke.{nested_name} must be a mapping"
            )
        fire_smoke[nested_name] = _merge_config(
            deepcopy(fire_smoke.get(nested_name, {}) or {}),
            deepcopy(nested_override),
        )

    smoking_override = functions.get("smoking", {}) or {}
    if not isinstance(smoking_override, dict):
        raise ValueError(f"camera {camera_id} analysis.functions.smoking must be a mapping")
    smoking = deepcopy(resolved.get("smoking_behavior", {}) or {})
    if "interval_ms" in smoking_override:
        smoking["interval_ms"] = int(smoking_override["interval_ms"])
    if "threshold" in smoking_override:
        smoking["smoking_threshold"] = float(smoking_override["threshold"])
    crop = smoking_override.get("crop", {}) or {}
    if not isinstance(crop, dict):
        raise ValueError(f"camera {camera_id} smoking.crop must be a mapping")
    strategy = str(crop.get("strategy", "person_padded"))
    if strategy != "person_padded":
        raise ValueError(
            f"camera {camera_id} smoking.crop.strategy must be person_padded"
        )
    smoking["crop_strategy"] = strategy
    if "padding_ratio" in crop:
        smoking["padding_ratio"] = float(crop["padding_ratio"])
    confirmation = smoking_override.get("confirmation", {}) or {}
    if not isinstance(confirmation, dict):
        raise ValueError(f"camera {camera_id} smoking.confirmation must be a mapping")
    temporal = deepcopy(smoking.get("temporal", {}) or {})
    for public_key, temporal_key in (
        ("hits", "confirmation_hits"),
        ("attempts", "confirmation_window"),
        ("clear_hits", "clear_negative_observations"),
    ):
        if public_key in confirmation:
            temporal[temporal_key] = int(confirmation[public_key])
    temporal_override = smoking_override.get("temporal", {}) or {}
    if not isinstance(temporal_override, dict):
        raise ValueError(f"camera {camera_id} smoking.temporal must be a mapping")
    smoking["temporal"] = _merge_config(temporal, temporal_override)
    lifecycle_override = smoking_override.get("lifecycle", {}) or {}
    if not isinstance(lifecycle_override, dict):
        raise ValueError(f"camera {camera_id} smoking.lifecycle must be a mapping")
    smoking["lifecycle"] = _merge_config(
        deepcopy(smoking.get("lifecycle", {}) or {}), lifecycle_override
    )

    for section_name, section in (("fire_smoke", fire_smoke), ("smoking", smoking)):
        interval_ms = int(section.get("interval_ms", 300))
        if not 50 <= interval_ms <= 60_000:
            raise ValueError(
                f"camera {camera_id} {section_name}.interval_ms must be in [50, 60000]"
            )
    for key in ("fire_threshold", "smoke_threshold"):
        value = float(fire_smoke.get(key, 0.0))
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"camera {camera_id} fire_smoke.{key} must be in [0, 1]")
    smoking_threshold = float(smoking.get("smoking_threshold", 0.0))
    if not 0.0 <= smoking_threshold <= 1.0:
        raise ValueError(f"camera {camera_id} smoking.threshold must be in [0, 1]")
    padding_ratio = float(smoking.get("padding_ratio", 0.20))
    if not 0.0 <= padding_ratio <= 1.0:
        raise ValueError(f"camera {camera_id} smoking.padding_ratio must be in [0, 1]")
    smoking_object = smoking.get("object_detection", {}) or {}
    if smoking_object.get("enabled", False):
        confidence = float(smoking_object.get("confidence", 0.35))
        nms_iou = float(smoking_object.get("nms_iou", 0.50))
        person_match_iou = float(smoking_object.get("person_match_iou", 0.10))
        if not 0.0 < confidence <= 1.0:
            raise ValueError(
                f"camera {camera_id} smoking.object_detection.confidence must be in (0, 1]"
            )
        if not 0.0 < nms_iou <= 1.0 or not 0.0 <= person_match_iou <= 1.0:
            raise ValueError(
                f"camera {camera_id} smoking object detection IoU values are invalid"
            )
        object_models = smoking_object.get("models", {}) or {}
        if not isinstance(object_models, dict) or not object_models:
            raise ValueError(
                f"camera {camera_id} smoking.object_detection.models must be a non-empty mapping"
            )
        for source, model in object_models.items():
            labels = [str(label) for label in (model or {}).get("labels", ())]
            positive_labels = [
                str(label) for label in (model or {}).get("positive_labels", ())
            ]
            if not labels or not positive_labels or not set(positive_labels).issubset(labels):
                raise ValueError(
                    f"camera {camera_id} smoking object model {source} has invalid labels"
                )
    for label, roi in (fire_smoke.get("class_rois", {}) or {}).items():
        if (
            not isinstance(roi, list | tuple)
            or len(roi) != 4
            or not all(0.0 <= float(value) <= 1.0 for value in roi)
            or float(roi[0]) >= float(roi[2])
            or float(roi[1]) >= float(roi[3])
        ):
            raise ValueError(
                f"camera {camera_id} fire_smoke.rois.{label} must be [left, top, right, bottom] in [0, 1]"
            )
    tracking = fire_smoke.get("tracking", {}) or {}
    tracking_hits = int(tracking.get("confirmation_hits", 4))
    tracking_window = int(tracking.get("confirmation_window", 6))
    if tracking_hits < 1 or tracking_window < tracking_hits:
        raise ValueError(
            f"camera {camera_id} fire_smoke tracking must satisfy confirmation_hits >= 1 and confirmation_window >= confirmation_hits"
        )
    for key, default in (
        ("match_iou", 0.10),
        ("match_center_distance", 0.20),
        ("bbox_smoothing_alpha", 0.35),
    ):
        value = float(tracking.get(key, default))
        if not 0.0 < value <= 1.0:
            raise ValueError(f"camera {camera_id} fire_smoke.tracking.{key} must be in (0, 1]")
    min_area_ratio = float(tracking.get("min_area_ratio", 0.25))
    max_area_ratio = float(tracking.get("max_area_ratio", 4.0))
    if min_area_ratio <= 0.0 or max_area_ratio < min_area_ratio:
        raise ValueError(
            f"camera {camera_id} fire_smoke tracking area ratio bounds are invalid"
        )
    if float(tracking.get("minimum_duration_seconds", 1.5)) < 0.0:
        raise ValueError(
            f"camera {camera_id} fire_smoke.tracking.minimum_duration_seconds must be non-negative"
        )
    notification_min_duration = float(
        tracking.get("notification_min_duration_seconds", 3.0)
    )
    if notification_min_duration < float(
        tracking.get("minimum_duration_seconds", 1.5)
    ):
        raise ValueError(
            f"camera {camera_id} fire_smoke.tracking.notification_min_duration_seconds must be at least minimum_duration_seconds"
        )
    if float(tracking.get("clear_seconds", 3.0)) <= 0.0:
        raise ValueError(
            f"camera {camera_id} fire_smoke.tracking.clear_seconds must be positive"
        )
    dynamics = fire_smoke.get("dynamics", {}) or {}
    if dynamics.get("enforce") is True:
        raise ValueError(
            f"camera {camera_id} fire_smoke dynamics hard enforcement is unsupported; use mode: advisory"
        )
    if str(dynamics.get("mode", "advisory")) != "advisory":
        raise ValueError(
            f"camera {camera_id} fire_smoke.dynamics.mode must be advisory"
        )
    crop_size = int(dynamics.get("crop_size", 96))
    if crop_size < 16 or crop_size > 512:
        raise ValueError(f"camera {camera_id} fire_smoke.dynamics.crop_size must be in [16, 512]")
    for key in (
        "crop_padding_ratio",
        "changed_pixel_ratio",
        "edge_change_ratio",
        "flow_circular_variance",
        "context_padding_ratio",
    ):
        value = float(dynamics.get(key, 0.0))
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"camera {camera_id} fire_smoke.dynamics.{key} must be in [0, 1]")
    if float(dynamics.get("changed_pixel_delta", 15.0)) <= 0.0:
        raise ValueError(f"camera {camera_id} fire_smoke.dynamics.changed_pixel_delta must be positive")
    if float(dynamics.get("flow_q75", 0.5)) < 0.0:
        raise ValueError(f"camera {camera_id} fire_smoke.dynamics.flow_q75 must be non-negative")
    required_conditions = int(dynamics.get("required_conditions", 2))
    dynamic_votes = int(dynamics.get("confirmation_votes", 3))
    dynamic_window = int(dynamics.get("confirmation_window", 5))
    if required_conditions not in {1, 2, 3}:
        raise ValueError(f"camera {camera_id} fire_smoke.dynamics.required_conditions must be in [1, 3]")
    if dynamic_votes < 1 or dynamic_window < dynamic_votes:
        raise ValueError(
            f"camera {camera_id} fire_smoke dynamics must satisfy confirmation_votes >= 1 and confirmation_window >= confirmation_votes"
        )
    canny_low = int(dynamics.get("canny_low", 50))
    canny_high = int(dynamics.get("canny_high", 150))
    if canny_low < 0 or canny_high <= canny_low:
        raise ValueError(f"camera {camera_id} fire_smoke dynamics Canny thresholds are invalid")
    temporal = smoking.get("temporal", {}) or {}
    lifecycle = smoking.get("lifecycle", {}) or {}
    hits = int(temporal.get("confirmation_hits", 2))
    attempts = int(temporal.get("confirmation_window", 4))
    clear_hits = int(temporal.get("clear_negative_observations", 4))
    if hits < 1 or attempts < hits or clear_hits < 1:
        raise ValueError(
            f"camera {camera_id} smoking confirmation must satisfy hits >= 1, attempts >= hits, clear_hits >= 1"
        )
    minimum_duration = float(temporal.get("minimum_duration_seconds", 0.4))
    candidate_timeout = float(lifecycle.get("candidate_timeout_seconds", 3.0))
    clearing_seconds = float(lifecycle.get("clearing_seconds", 3.0))
    notification_min_duration = float(
        lifecycle.get("notification_min_duration_seconds", 3.0)
    )
    trace_interval_ms = int(lifecycle.get("trace_interval_ms", 400))
    if minimum_duration < 0.0:
        raise ValueError(
            f"camera {camera_id} smoking.temporal.minimum_duration_seconds must be non-negative"
        )
    if candidate_timeout <= 0.0 or clearing_seconds <= 0.0:
        raise ValueError(
            f"camera {camera_id} smoking lifecycle timeouts must be positive"
        )
    if notification_min_duration < minimum_duration:
        raise ValueError(
            f"camera {camera_id} smoking.lifecycle.notification_min_duration_seconds must be at least minimum_duration_seconds"
        )
    if trace_interval_ms < 50:
        raise ValueError(
            f"camera {camera_id} smoking.lifecycle.trace_interval_ms must be at least 50"
        )
    resolved["fire_smoke"] = fire_smoke
    resolved["smoking_behavior"] = smoking


def resolve_camera_config(config: dict[str, Any], camera_id: str | None = None) -> dict[str, Any]:
    """Normalize legacy single-input or new multi-camera config to one worker."""
    ids = camera_ids(config)
    if camera_id is None:
        if len(ids) != 1:
            raise ValueError(
                "camera_id is required when the config contains multiple cameras"
            )
        camera_id = ids[0]
    camera = _camera_by_id(config, camera_id) if config.get("cameras") else {}
    resolved = deepcopy(config)
    resolved.pop("cameras", None)

    source = camera.get("source", {}) or {}
    if not isinstance(source, dict):
        raise ValueError(f"camera {camera_id} source must be a mapping")
    input_config = deepcopy(resolved.get("input", {}) or {})
    input_config.update(
        {
            "mode": source.get("type", source.get("mode", input_config.get("mode", "rtsp"))),
            "camera": camera_id,
            "rtsp_url": source.get("url", source.get("rtsp_url", input_config.get("rtsp_url"))),
            "mock_video": source.get("mock_video", input_config.get("mock_video")),
            "mock_loop": source.get("loop", source.get("mock_loop", input_config.get("mock_loop", True))),
            "media_only": bool(source.get("media_only", input_config.get("media_only", False))),
            "mock_sync_group": str(source.get("sync_group", input_config.get("mock_sync_group", ""))),
            "mock_sync_period_seconds": float(
                source.get("sync_period_seconds", input_config.get("mock_sync_period_seconds", 0.0))
            ),
            "mock_sync_epoch_seconds": float(
                source.get("sync_epoch_seconds", input_config.get("mock_sync_epoch_seconds", 0.0))
            ),
            "width": int(source.get("width", input_config.get("width", 1920))),
            "height": int(source.get("height", input_config.get("height", 1080))),
            "latency_ms": int(source.get("latency_ms", input_config.get("latency_ms", 200))),
            "codec": str(source.get("codec", input_config.get("codec", "h264"))),
            "decoder": str(source.get("decoder", input_config.get("decoder", "hardware"))),
        }
    )
    runtime_environment = _runtime_environment()
    username_env = source.get("username_env")
    password_env = source.get("password_env")
    if username_env:
        input_config["rtsp_username"] = runtime_environment.get(str(username_env), "")
    if password_env:
        input_config["rtsp_password"] = runtime_environment.get(str(password_env), "")
    if not input_config.get("rtsp_url"):
        raise ValueError(f"camera {camera_id} source must define url/rtsp_url")
    input_base = runtime_environment.get("CAMERA_INPUT_RTSP_BASE", "").rstrip("/")
    input_url = str(input_config["rtsp_url"])
    if input_base and urlsplit(input_url).hostname in {"127.0.0.1", "localhost"}:
        parsed_base = urlsplit(input_base)
        if parsed_base.scheme not in {"rtsp", "rtsps"} or not parsed_base.netloc:
            raise ValueError("CAMERA_INPUT_RTSP_BASE must be a valid RTSP URL")
        input_path = urlsplit(input_url).path.strip("/")
        input_config["rtsp_url"] = f"{input_base}/{input_path}"
    resolved["input"] = input_config

    metadata = deepcopy(resolved.get("metadata", {}) or {})
    metadata["zmq_pub_url"] = _camera_metadata_url(config, camera_id, ids.index(camera_id))
    resolved["metadata"] = metadata

    output = deepcopy(resolved.get("output", {}) or {})
    output.update(camera.get("output", {}) or {})
    if not output.get("rtsp_url"):
        raise ValueError(f"camera {camera_id} output must define rtsp_url")
    output_base = runtime_environment.get("CAMERA_OUTPUT_RTSP_BASE", "").rstrip("/")
    if output_base:
        parsed_base = urlsplit(output_base)
        if parsed_base.scheme not in {"rtsp", "rtsps", "rtmp"} or not parsed_base.netloc:
            raise ValueError("CAMERA_OUTPUT_RTSP_BASE must be a valid streaming URL")
        output_path = urlsplit(str(output["rtsp_url"])).path.strip("/")
        output["rtsp_url"] = f"{output_base}/{output_path}"
    resolved["output"] = output

    functions = {
        "trace": True,
        "face_recognition": bool((resolved.get("recognition", {}) or {}).get("enabled", False)),
        "smoking_behavior": bool(
            (resolved.get("smoking_behavior", {}) or {}).get("enabled", False)
        ),
        "fire_smoke": bool((resolved.get("fire_smoke", {}) or {}).get("enabled", False)),
        "dms": bool((resolved.get("dms", {}) or {}).get("enabled", False)),
        "front_assistance": bool(
            (resolved.get("front_assistance", {}) or {}).get("enabled", False)
        ),
    }
    functions.update(camera.get("functions", {}) or {})
    resolved["functions"] = functions

    recognition = deepcopy(resolved.get("recognition", {}) or {})
    recognition["enabled"] = bool(functions["face_recognition"])
    face_runtime = deepcopy(recognition.get("face_runtime", {}) or {})
    face_runtime["trace_enabled"] = bool(functions["trace"])
    if runtime_environment.get("CAMERA_FACE_LIBRARY_DIR"):
        face_runtime["library_directory"] = runtime_environment["CAMERA_FACE_LIBRARY_DIR"]
    recognition["face_runtime"] = face_runtime
    resolved["recognition"] = recognition

    notifications = deepcopy(resolved.get("notifications", {}) or {})
    notification_override = _environment_bool(
        runtime_environment, "DEEPSTREAM_NOTIFICATIONS_ENABLED"
    )
    if notification_override is not None:
        notifications["enabled"] = notification_override
    resolved["notifications"] = notifications

    smoking_behavior = deepcopy(resolved.get("smoking_behavior", {}) or {})
    smoking_behavior["enabled"] = bool(functions["smoking_behavior"])
    resolved["smoking_behavior"] = smoking_behavior

    fire_smoke = deepcopy(resolved.get("fire_smoke", {}) or {})
    fire_smoke["enabled"] = bool(functions["fire_smoke"])
    resolved["fire_smoke"] = fire_smoke

    dms = deepcopy(resolved.get("dms", {}) or {})
    dms["enabled"] = bool(functions["dms"])
    resolved["dms"] = dms

    front_assistance = deepcopy(resolved.get("front_assistance", {}) or {})
    front_assistance.update(camera.get("front_assistance", {}) or {})
    front_assistance["enabled"] = bool(functions["front_assistance"])
    resolved["front_assistance"] = front_assistance

    _normalize_camera_analysis(resolved, camera, camera_id)

    person = deepcopy(resolved.get("person", {}) or {})
    person_tracking = deepcopy(person.get("tracking", {}) or {})
    confirmation_hits = int(person_tracking.get("confirmation_hits", 2))
    confirmation_window = int(person_tracking.get("confirmation_window", 4))
    if confirmation_hits < 1:
        raise ValueError("person.tracking.confirmation_hits must be at least 1")
    if confirmation_window < confirmation_hits:
        raise ValueError(
            "person.tracking.confirmation_window must be at least confirmation_hits"
        )
    person_tracking["confirmation_hits"] = confirmation_hits
    person_tracking["confirmation_window"] = confirmation_window
    person["tracking"] = person_tracking
    resolved["person"] = person

    # SmokingEpisodeStore is the only owner of smoking temporal/event state.
    # Do not reintroduce the former generic camera-level events gate here.
    resolved.pop("events", None)

    snapshots = deepcopy(resolved.get("snapshots", {}) or {})
    snapshots.pop("directory", None)
    resolved["snapshots"] = snapshots
    return resolved


def load_config(path: Path, camera_id: str | None = None) -> dict[str, Any]:
    raw = validate_config(load_raw_config(path), path)
    return resolve_camera_config(raw, camera_id)
