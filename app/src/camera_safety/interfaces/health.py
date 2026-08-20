"""Health state model with separate liveness and readiness gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class HealthState:
    process_alive: bool = True
    models_loaded: bool = False
    gpu_provider_active: bool = False
    input_frame_fresh: bool = False
    output_frame_fresh: bool = False
    analysis_result_fresh: bool = False
    evidence_writable: bool = False
    notification_outbox_healthy: bool = False

    def live(self) -> bool:
        return self.process_alive

    def ready(self) -> bool:
        return all(asdict(self).values())

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "live": self.live(), "ready": self.ready()}
