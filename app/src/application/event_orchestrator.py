"""Event transition port; persistence and notification stay outside probes."""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.contracts import EventContract, Lifecycle


@dataclass
class EventOrchestrator:
    active: dict[str, EventContract] = field(default_factory=dict)

    def start(self, event: EventContract) -> EventContract:
        if event.lifecycle is not Lifecycle.START:
            raise ValueError("event start must use START lifecycle")
        self.active[event.event_id] = event
        return event

    def end(self, event_id: str) -> EventContract | None:
        current = self.active.pop(event_id, None)
        if current is None:
            return None
        return EventContract(**{**current.__dict__, "lifecycle": Lifecycle.END})
