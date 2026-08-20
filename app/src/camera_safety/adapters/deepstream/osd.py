"""Annotation value boundary for DeepStream OSD adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Overlay:
    label: str
    bbox: tuple[float, float, float, float]
    score: float | None = None
