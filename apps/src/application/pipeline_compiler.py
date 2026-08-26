"""Compile resolved camera configuration into immutable execution plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from application.function_registry import FUNCTION_REGISTRY, registration_for
from domain.pipeline_plan import CameraExecutionPlan, FunctionSpec


@lru_cache(maxsize=128)
def _sha256_for_stat(path_text: str, modified_ns: int, size: int) -> str:
    del modified_ns, size
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_revision(name: str, config: dict[str, Any]) -> str | None:
    paths = registration_for(name).model_resolver(config)
    revisions = []
    for value in paths:
        path = Path(value)
        try:
            stat = path.stat()
            checksum = _sha256_for_stat(str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            checksum = "unavailable"
        revisions.append(f"{value}#sha256:{checksum}")
    return "|".join(revisions) or None


def _safe_config(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    secret_key = lowered in {"password", "secret", "token", "api_key"} or lowered.endswith(
        ("_password", "_secret", "_token", "_api_key")
    )
    if secret_key and not lowered.endswith("_env"):
        return "<configured>" if value else ""
    if isinstance(value, dict):
        return {
            str(child_key): _safe_config(child_value, str(child_key))
            for child_key, child_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_safe_config(item) for item in value]
    return value


def compile_camera_plan(config: dict[str, Any]) -> CameraExecutionPlan:
    input_config = config.get("input", {}) or {}
    camera_id = str(input_config.get("camera", "camera"))
    media_only = bool(input_config.get("media_only", False))
    configured_functions = config.get("functions", {}) or {}
    unknown = sorted(
        name
        for name, enabled in configured_functions.items()
        if name != "trace" and bool(enabled) and name not in FUNCTION_REGISTRY
    )
    if unknown:
        raise ValueError(
            f"camera {camera_id} enables unregistered functions: {', '.join(unknown)}"
        )
    function_specs = tuple(
        FunctionSpec(
            name=name,
            interval_seconds=registration_for(name).interval_resolver(config),
            model_revision=_model_revision(name, config),
        )
        for name in FUNCTION_REGISTRY
        if bool(configured_functions.get(name, False))
    )
    if media_only and function_specs:
        raise ValueError(f"camera {camera_id} media_only cannot have an analysis plan")
    shared_nodes = tuple(
        sorted(
            {
                node
                for function in function_specs
                for node in registration_for(function.name).shared_nodes
            }
        )
    )
    estimated_inference_rate_hz = round(
        sum(1.0 / item.interval_seconds for item in function_specs), 3
    )
    runtime = config.get("runtime", {}) or {}
    budget = runtime.get("dynamic_pipeline", {}) or {}
    max_functions = int(budget.get("max_functions_per_camera", 0) or 0)
    max_rate_hz = float(budget.get("max_inference_rate_hz", 0.0) or 0.0)
    resource_warnings = []
    if max_functions and len(function_specs) > max_functions:
        resource_warnings.append(
            f"enabled function count {len(function_specs)} exceeds budget {max_functions}"
        )
    if max_rate_hz and estimated_inference_rate_hz > max_rate_hz:
        resource_warnings.append(
            f"estimated inference rate {estimated_inference_rate_hz:.3f}Hz "
            f"exceeds budget {max_rate_hz:.3f}Hz"
        )
    if resource_warnings and bool(budget.get("enforce", True)):
        raise ValueError(
            f"camera {camera_id} exceeds dynamic pipeline resource budget: "
            + "; ".join(resource_warnings)
        )
    sync_group = str(input_config.get("mock_sync_group", "")).strip()
    timeline_contract = (
        (
            sync_group,
            float(input_config.get("mock_sync_period_seconds", 0.0)),
            float(input_config.get("mock_sync_epoch_seconds", 0.0)),
            media_only,
            str(input_config.get("mock_video", "")),
            str(input_config.get("rtsp_url", "")),
        )
        if sync_group
        else None
    )
    fingerprint = {
        "camera_id": camera_id,
        "media_only": media_only,
        "input": _safe_config(input_config),
        "output": _safe_config(config.get("output", {}) or {}),
        "functions": [asdict(item) for item in function_specs],
        "shared_nodes": shared_nodes,
        "estimated_inference_rate_hz": estimated_inference_rate_hz,
        "resource_budget": _safe_config(budget),
        "trace": bool(configured_functions.get("trace", True)),
        "analysis": _safe_config(config.get("analysis", {}) or {}),
        "person": (
            _safe_config(config.get("person", {}) or {})
            if "person_inference" in shared_nodes
            else None
        ),
        "services": {
            name: _safe_config(config.get(name, {}) or {})
            for name in ("notifications", "evidence", "snapshots")
        },
        "function_config": {
            item.name: _safe_config(
                config.get(registration_for(item.name).config_section, {}) or {}
            )
            for item in function_specs
        },
    }
    encoded = json.dumps(
        fingerprint,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return CameraExecutionPlan(
        camera_id=camera_id,
        media_only=media_only,
        functions=function_specs,
        shared_nodes=shared_nodes,
        estimated_inference_rate_hz=estimated_inference_rate_hz,
        resource_warnings=tuple(resource_warnings),
        plan_hash=hashlib.sha256(encoded).hexdigest(),
        timeline_contract=timeline_contract,
    )
