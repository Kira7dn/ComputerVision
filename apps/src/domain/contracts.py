"""Typed boundaries shared by inference, lifecycle, and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Lifecycle(str, Enum):
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


@dataclass(frozen=True, order=True)
class FrameKey:
    """Stable identity for one decoded source frame within a worker run."""

    run_id: str
    camera_id: str
    source_id: int
    frame_number: int
    buffer_pts_ns: int | None = field(default=None, compare=False)

    @property
    def ordering_value(self) -> tuple[int, int]:
        # Decoder PTS can move backwards around RTCP resynchronisation and
        # reordered video frames.  DeepStream's frame number is monotonic for
        # one source within one worker epoch, so use it as the result gate's
        # canonical ordering key and retain PTS as timestamp metadata only.
        return (self.source_id, self.frame_number)


@dataclass(frozen=True)
class AnalysisSample:
    """Immutable frame and person tracks shared by independent analyzers."""

    key: FrameKey
    source_timestamp: float
    captured_monotonic: float
    frame: Any
    persons: tuple[tuple[int, float, float, float, float], ...] = ()


@dataclass(frozen=True)
class FunctionResult:
    """Frame-correlated result produced by one analysis function."""

    function: str
    key: FrameKey
    detections: tuple[Any, ...]
    transitions: tuple[Any, ...] = ()
    started_monotonic: float = 0.0
    finished_monotonic: float = 0.0
    model_revision: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def inference_seconds(self) -> float:
        return max(0.0, self.finished_monotonic - self.started_monotonic)


@dataclass(frozen=True)
class DetectionResult:
    function: str
    classification: str
    score: float
    bbox: tuple[float, float, float, float] | None = None
    track_id: int | None = None
    frame_number: int | None = None
    frame_key: FrameKey | None = None
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    model_revision: str | None = None
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
