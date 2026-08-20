"""Application boundary for model results.

The live DeepStream worker remains the compatibility implementation while this
port makes the decision flow explicit and testable without importing GStreamer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from camera_safety.domain.contracts import DetectionResult


@dataclass
class InferenceOrchestrator:
    def process(self, results: Iterable[DetectionResult]) -> list[DetectionResult]:
        return list(results)
