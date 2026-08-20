"""Application port ensuring evidence exists before downstream notification."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class EvidenceService(Protocol):
    def start_event(self, **event: Any) -> str: ...
    def event_directory(self, event_id: str) -> Path | None: ...


def evidence_ready(event_directory: Path | None) -> bool:
    return bool(event_directory and (event_directory / "event.json").is_file())
