"""Ports owned by the domain and implemented by outer adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class EvidencePort(Protocol):
    worker_epoch: str

    def event_directory(self, event_id: str) -> Path | None: ...

    def start_event(self, **event: Any) -> str: ...

    def record(
        self, event_id: str, record_type: str, payload: dict[str, Any], **evidence: Any
    ) -> bool: ...

    def finish_event(
        self,
        event_id: str,
        *,
        classification: str | None = None,
        identity: str | None = None,
        payload: dict[str, Any] | None = None,
        frame: Any = None,
        frame_number: int | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        score: float | None = None,
    ) -> None: ...
