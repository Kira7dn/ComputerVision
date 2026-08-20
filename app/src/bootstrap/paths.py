"""Runtime paths; source code never owns production data directories."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    models: Path
    face_library: Path
    evidence: Path
    state: Path
    queue: Path
    logs: Path
    status: Path

    @classmethod
    def from_environment(cls) -> RuntimePaths:
        root = Path(os.environ.get("CAMERA_RUNTIME_ROOT", "/opt/camera-safety"))
        return cls(
            models=Path(os.environ.get("CAMERA_MODELS_DIR", root / "models")),
            face_library=Path(os.environ.get("CAMERA_FACE_LIBRARY_DIR", root / "face_library")),
            evidence=Path(os.environ.get("CAMERA_EVIDENCE_DIR", root / "evidence")),
            state=Path(os.environ.get("CAMERA_STATE_DIR", root / "state")),
            queue=Path(os.environ.get("CAMERA_QUEUE_DIR", root / "queue")),
            logs=Path(os.environ.get("CAMERA_LOG_DIR", root / "logs")),
            status=Path(os.environ.get("CAMERA_STATUS_DIR", root / "status")),
        )

    def ensure_writable(self) -> None:
        for path in (self.evidence, self.state, self.queue, self.logs, self.status):
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".camera-safety-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
