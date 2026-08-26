from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from application.pipeline_compiler import compile_camera_plan
from bootstrap.config import load_raw_config, resolve_camera_config

ROOT = Path(__file__).parents[2]


def _resolved(camera_id: str) -> dict:
    return resolve_camera_config(
        load_raw_config(ROOT / "config" / "production.yaml"), camera_id
    )


def test_compiles_dms_dependencies_and_stable_hash() -> None:
    config = _resolved("DMS")

    first = compile_camera_plan(config)
    second = compile_camera_plan(deepcopy(config))

    assert first.enabled_functions == ("dms",)
    assert first.shared_nodes == ("cpu_frame", "person_inference")
    assert first.estimated_inference_rate_hz == 10.0
    assert first.functions[0].model_revision is not None
    assert first.plan_hash == second.plan_hash


def test_front_plan_does_not_require_person_inference() -> None:
    plan = compile_camera_plan(_resolved("camera_front"))

    assert plan.enabled_functions == ("front_assistance",)
    assert plan.shared_nodes == ("cpu_frame",)
    assert plan.timeline_contract[:3] == ("vehicle_surround", 191.1, 0.0)


def test_relevant_override_changes_only_camera_plan_hash() -> None:
    config = _resolved("DMS")
    before = compile_camera_plan(config)
    config["dms"]["interval_ms"] = int(config["dms"]["interval_ms"]) + 50

    assert compile_camera_plan(config).plan_hash != before.plan_hash


def test_rejects_unregistered_enabled_function() -> None:
    config = _resolved("DMS")
    config["functions"]["unknown"] = True

    with pytest.raises(ValueError, match="unregistered functions: unknown"):
        compile_camera_plan(config)


def test_camera_local_dms_override_does_not_change_other_camera() -> None:
    raw = load_raw_config(ROOT / "config" / "production.yaml")
    dms_camera = next(item for item in raw["cameras"] if item["id"] == "DMS")
    dms_camera["dms"] = {"attention": {"interval_ms": 175}}

    dms = resolve_camera_config(raw, "DMS")
    front = resolve_camera_config(raw, "camera_front")

    assert dms["dms"]["attention"]["interval_ms"] == 175
    assert front["dms"]["attention"]["interval_ms"] != 175


def test_resource_budget_fails_closed() -> None:
    config = _resolved("DMS")
    config["runtime"]["dynamic_pipeline"] = {
        "enforce": True,
        "max_functions_per_camera": 1,
        "max_inference_rate_hz": 5.0,
    }

    with pytest.raises(ValueError, match="resource budget"):
        compile_camera_plan(config)


def test_resource_budget_can_report_warning_without_enforcement() -> None:
    config = _resolved("DMS")
    config["runtime"]["dynamic_pipeline"] = {
        "enforce": False,
        "max_inference_rate_hz": 5.0,
    }

    plan = compile_camera_plan(config)

    assert plan.resource_warnings


def test_model_revision_contains_content_checksum(tmp_path: Path) -> None:
    model = tmp_path / "dms.onnx"
    model.write_bytes(b"model-revision")
    config = _resolved("DMS")
    config["dms"]["object_detection"]["models"] = {
        "test": {"onnx_path": str(model)}
    }

    plan = compile_camera_plan(config)

    expected = hashlib.sha256(b"model-revision").hexdigest()
    assert f"sha256:{expected}" in str(plan.functions[0].model_revision)
