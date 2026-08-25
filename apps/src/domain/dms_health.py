"""Fail-closed DMS topology and operator health semantics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DmsHealth:
    status: str
    message: str | None
    observation_ready: bool
    driver_visible: bool
    face_visible: bool


def requires_person_inference(functions: Mapping[str, Any]) -> bool:
    """Return whether a camera function needs the shared person tracker."""
    return any(
        bool(functions.get(name, False))
        for name in ("face_recognition", "smoking_behavior", "fire_smoke", "dms")
    )


def resolve_dms_health(
    engine_status: str,
    confirmed_alerts: Iterable[str],
    metrics: Mapping[str, Any],
    engine_message: str | None = None,
) -> DmsHealth:
    """Describe what DMS can currently observe without reporting a false OK."""
    alerts = tuple(str(item) for item in confirmed_alerts if str(item))
    driver_visible = int(metrics.get("driver_person_count", 0) or 0) > 0
    face_visible = metrics.get("face_detected") is True

    if engine_status in {"DISABLED", "DEGRADED"}:
        return DmsHealth(
            status=engine_status,
            message=engine_message,
            observation_ready=False,
            driver_visible=driver_visible,
            face_visible=face_visible,
        )
    if alerts:
        return DmsHealth(
            status="ALERT",
            message=engine_message,
            observation_ready=True,
            driver_visible=driver_visible,
            face_visible=face_visible,
        )
    if driver_visible and face_visible:
        return DmsHealth(
            status="MONITORING",
            message=engine_message,
            observation_ready=True,
            driver_visible=True,
            face_visible=True,
        )
    if driver_visible:
        return DmsHealth(
            status="PARTIAL",
            message=engine_message or "driver face not detected",
            observation_ready=True,
            driver_visible=True,
            face_visible=False,
        )
    if face_visible:
        return DmsHealth(
            status="PARTIAL",
            message=engine_message or "driver person track unavailable",
            observation_ready=False,
            driver_visible=False,
            face_visible=True,
        )
    return DmsHealth(
        status="NO_DRIVER",
        message=engine_message or "driver not detected",
        observation_ready=False,
        driver_visible=False,
        face_visible=False,
    )
