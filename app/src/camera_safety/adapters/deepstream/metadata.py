"""Live metadata publishing port."""

from __future__ import annotations

from typing import Any, Protocol


class MetadataPublisher(Protocol):
    def publish(self, payload: dict[str, Any]) -> None: ...
