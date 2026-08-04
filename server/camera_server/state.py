"""Thread-safe bounded runtime state and event revisions."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class RuntimeState:
    def __init__(self, max_events: int = 256):
        self._lock = threading.RLock()
        self._revision = 0
        self._snapshot: dict[str, Any] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)

    def update(self, event_type: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            self._revision += 1
            self._snapshot.update(changes)
            event = {"type": event_type, "revision": self._revision, **changes}
            self._events.append(event)
            return dict(event)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"revision": self._revision, **self._snapshot}

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)
