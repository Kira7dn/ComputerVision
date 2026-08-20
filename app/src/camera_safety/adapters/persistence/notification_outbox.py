"""Durable notification outbox port."""

from __future__ import annotations

from typing import Any, Protocol


class NotificationOutbox(Protocol):
    def enqueue(self, event: dict[str, Any]) -> None: ...

    def health(self) -> dict[str, Any]: ...
