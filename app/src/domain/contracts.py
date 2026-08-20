"""Typed boundaries shared by inference, lifecycle, and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Lifecycle(StrEnum):
    START = "START"
    UPDATE = "UPDATE"
    END = "END"


@dataclass(frozen=True)
class EvidenceReference:
    event_id: str
    relative_path: str
    original_path: str | None = None
    thumbnail_path: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class DetectionResult:
    function: str
    classification: str
    score: float
    bbox: tuple[float, float, float, float] | None = None
    track_id: int | None = None
    frame_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventContract:
    event_id: str
    camera_id: str
    function: str
    classification: str
    lifecycle: Lifecycle
    evidence: EvidenceReference | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
