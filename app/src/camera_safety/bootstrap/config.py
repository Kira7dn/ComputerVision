"""Configuration loading and per-camera normalization for DeepStream Safety."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml


def load_raw_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError("DeepStream config root must be a mapping")
    extends = data.pop("extends", None)
    if extends:
        base_path = (path.parent / str(extends)).resolve()
        if base_path == path.resolve():
            raise ValueError("config cannot extend itself")
        data = _merge_config(load_raw_config(base_path), data)
    return data


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
    ids = camera_ids(config)
    if not ids:
        raise ValueError("at least one camera is required")
    for camera in config.get("cameras", []) or []:
        camera_id = str(camera["id"])
        source = camera.get("source", {}) or {}
        source_type = str(source.get("type", source.get("mode", "rtsp"))).lower()
        if profile == "production" and source_type == "mock":
            raise ValueError(f"production profile cannot use mock source: {camera_id}")
        source_url = str(source.get("url", source.get("rtsp_url", "")))
        output_url = str((camera.get("output", {}) or {}).get("rtsp_url", ""))
        for label, value in (("source", source_url), ("output", output_url)):
            parsed = urlsplit(value)
            if parsed.scheme not in {"rtsp", "rtsps", "rtmp"} or not parsed.netloc:
                raise ValueError(f"camera {camera_id} {label} URL is invalid: {value}")

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
    if validate_models:
        missing = [str(item) for item in model_paths if item and not Path(str(item)).is_file()]
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
    url = metadata.get("zmq_pub_url")
    if not url:
        url = "tcp://127.0.0.1:5555"
    if not url or camera_metadata.get("zmq_pub_url"):
        return url
    parsed = urlsplit(str(url))
    if parsed.port is None:
        return str(url)
    return urlunsplit(
        (parsed.scheme, f"{parsed.hostname}:{parsed.port + index}", parsed.path, parsed.query, parsed.fragment)
    )


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
            "width": int(source.get("width", input_config.get("width", 1920))),
            "height": int(source.get("height", input_config.get("height", 1080))),
            "latency_ms": int(source.get("latency_ms", input_config.get("latency_ms", 200))),
            "codec": str(source.get("codec", input_config.get("codec", "h264"))),
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
    resolved["input"] = input_config

    metadata = deepcopy(resolved.get("metadata", {}) or {})
    metadata["zmq_pub_url"] = _camera_metadata_url(config, camera_id, ids.index(camera_id))
    resolved["metadata"] = metadata

    output = deepcopy(resolved.get("output", {}) or {})
    output.update(camera.get("output", {}) or {})
    if not output.get("rtsp_url"):
        raise ValueError(f"camera {camera_id} output must define rtsp_url")
    resolved["output"] = output

    functions = {
        "trace": True,
        "face_recognition": bool((resolved.get("recognition", {}) or {}).get("enabled", False)),
        "smoking_behavior": bool(
            (resolved.get("smoking_behavior", {}) or {}).get("enabled", False)
        ),
        "fire_smoke": bool((resolved.get("fire_smoke", {}) or {}).get("enabled", False)),
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

    person = deepcopy(resolved.get("person", {}) or {})
    person_tracking = deepcopy(person.get("tracking", {}) or {})
    confirmation_hits = int(person_tracking.get("confirmation_hits", 2))
    confirmation_window = int(person_tracking.get("confirmation_window", 4))
    fire_smoke_overlap_ratio = float(
        person_tracking.get("fire_smoke_exclusion_overlap_ratio", 0.25)
    )
    if confirmation_hits < 1:
        raise ValueError("person.tracking.confirmation_hits must be at least 1")
    if confirmation_window < confirmation_hits:
        raise ValueError(
            "person.tracking.confirmation_window must be at least confirmation_hits"
        )
    if not 0.0 < fire_smoke_overlap_ratio <= 1.0:
        raise ValueError(
            "person.tracking.fire_smoke_exclusion_overlap_ratio must be in (0, 1]"
        )
    person_tracking["confirmation_hits"] = confirmation_hits
    person_tracking["confirmation_window"] = confirmation_window
    person_tracking["fire_smoke_exclusion_overlap_ratio"] = fire_smoke_overlap_ratio
    person["tracking"] = person_tracking
    resolved["person"] = person

    events = deepcopy(resolved.get("events", {}) or {})
    events.update(camera.get("events", {}) or {})
    events["camera"] = camera_id
    events["enabled"] = bool(functions["smoking_behavior"]) and bool(
        events.get("enabled", True)
    )
    events["trace_enabled"] = bool(functions["trace"])
    events.pop("directory", None)
    resolved["events"] = events

    snapshots = deepcopy(resolved.get("snapshots", {}) or {})
    snapshots.pop("directory", None)
    resolved["snapshots"] = snapshots
    return resolved


def load_config(path: Path, camera_id: str | None = None) -> dict[str, Any]:
    raw = validate_config(load_raw_config(path), path)
    return resolve_camera_config(raw, camera_id)
