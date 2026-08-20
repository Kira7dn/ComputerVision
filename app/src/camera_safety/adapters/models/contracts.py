"""Common model adapter lifecycle contract."""

from __future__ import annotations

from typing import Any, Protocol

from camera_safety.domain.contracts import DetectionResult


class ModelAdapter(Protocol):
    def load(self) -> None: ...
    def health(self) -> dict[str, Any]: ...
    def process(self, frame: Any, context: dict[str, Any]) -> list[DetectionResult]: ...
    def close(self) -> None: ...
