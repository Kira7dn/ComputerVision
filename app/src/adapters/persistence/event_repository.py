"""Read/write event repository port for the dashboard read model."""

from __future__ import annotations

from typing import Any, Protocol


class EventRepository(Protocol):
    def append(self, event: dict[str, Any]) -> None: ...
    def list_events(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]: ...
