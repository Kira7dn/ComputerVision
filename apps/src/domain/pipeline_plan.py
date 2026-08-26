"""Immutable execution plan contracts for one camera runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    name: str
    interval_seconds: float
    model_revision: str | None


@dataclass(frozen=True, slots=True)
class CameraExecutionPlan:
    camera_id: str
    media_only: bool
    functions: tuple[FunctionSpec, ...]
    shared_nodes: tuple[str, ...]
    estimated_inference_rate_hz: float
    resource_warnings: tuple[str, ...]
    plan_hash: str
    timeline_contract: tuple[str, float, float, bool, str, str] | None

    @property
    def enabled_functions(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.functions)

    def status(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "enabled_functions": list(self.enabled_functions),
            "shared_nodes": list(self.shared_nodes),
            "estimated_inference_rate_hz": self.estimated_inference_rate_hz,
            "model_revisions": {
                item.name: item.model_revision for item in self.functions
            },
            "resource_warnings": list(self.resource_warnings),
        }
