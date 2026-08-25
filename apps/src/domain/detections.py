"""Small domain detection value objects kept independent of DeepStream."""

from dataclasses import dataclass

from .contracts import DetectionResult


@dataclass(frozen=True)
class FireSmokeDetection:
    label: str
    score: float
    bbox: tuple[float, float, float, float]


__all__ = ["DetectionResult", "FireSmokeDetection"]
