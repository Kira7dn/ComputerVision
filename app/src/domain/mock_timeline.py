"""Deterministic wall-clock timeline mapping for synchronized camera fixtures."""

from __future__ import annotations

import math


def timeline_phase_seconds(
    timestamp: float,
    period_seconds: float,
    epoch_seconds: float = 0.0,
) -> float:
    """Return the shared, wrapped playback phase for an absolute timestamp."""
    if not math.isfinite(timestamp):
        raise ValueError("timestamp must be finite")
    if not math.isfinite(period_seconds) or period_seconds <= 0.0:
        raise ValueError("period_seconds must be finite and positive")
    if not math.isfinite(epoch_seconds):
        raise ValueError("epoch_seconds must be finite")
    return (timestamp - epoch_seconds) % period_seconds


def normalized_timeline_phase(
    timestamp: float,
    period_seconds: float,
    epoch_seconds: float = 0.0,
) -> float:
    """Return a group phase in the half-open range [0, 1)."""
    return timeline_phase_seconds(timestamp, period_seconds, epoch_seconds) / period_seconds


def frame_index_for_timestamp(
    timestamp: float,
    period_seconds: float,
    frame_count: int,
    epoch_seconds: float = 0.0,
) -> int:
    """Map a group timestamp to the corresponding frame in one camera file."""
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    phase = normalized_timeline_phase(timestamp, period_seconds, epoch_seconds)
    return min(frame_count - 1, int(phase * frame_count))


def media_time_for_timestamp(
    timestamp: float,
    period_seconds: float,
    duration_seconds: float,
    epoch_seconds: float = 0.0,
) -> float:
    """Map a group timestamp to media time while tolerating duration variance."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0.0:
        raise ValueError("duration_seconds must be finite and positive")
    return normalized_timeline_phase(timestamp, period_seconds, epoch_seconds) * duration_seconds
