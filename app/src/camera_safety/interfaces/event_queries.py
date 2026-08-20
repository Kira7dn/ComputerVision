"""Read-model port for dashboard event queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class EventQueryService(Protocol):
    def list_events(self, after: int = 0, limit: int | None = None) -> dict[str, Any]: ...

    def thumbnail(self, run_id: str, event_path: str, variant: str = "thumbnail") -> Path | None: ...
