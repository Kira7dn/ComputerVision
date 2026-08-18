"""Standalone Safety event state, trace, and event-owned snapshots."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class EventState(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    ACTIVE = "active"


@dataclass(frozen=True)
class EventTransition:
    operation: str
    event_id: str
    timestamp: float
    frame_num: int
    score: float
    bbox: tuple[float, float, float, float] | None


class SafetyEventStore:
    """Keep temporal event state and durable local trace without Frigate imports."""

    def __init__(self, config: dict[str, Any]) -> None:
        event_cfg = config.get("events", {})
        self.enabled = bool(event_cfg.get("enabled", True))
        self.camera = str(event_cfg.get("camera", "safety_mock"))
        self.label = str(config.get("model", {}).get("label", "cigarette"))
        self.confirm_seconds = float(event_cfg.get("confirm_seconds", 1.0))
        self.clear_seconds = float(event_cfg.get("clear_seconds", 5.0))
        self.root = Path(event_cfg.get("directory", "/tmp/deepstream-safety/events"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.root / "runtime-trace.jsonl"
        self.state = EventState.IDLE
        self.active_event_id: str | None = None
        self.candidate_since: float | None = None
        self.clear_since: float | None = None
        self.last_score = 0.0
        self.last_bbox: tuple[float, float, float, float] | None = None
        self.event_count = 0
        self._event: dict[str, Any] | None = None

    @staticmethod
    def _bbox(values: Any) -> tuple[float, float, float, float] | None:
        if values is None or len(values) != 4:
            return None
        return tuple(round(float(value), 6) for value in values)  # type: ignore[return-value]

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def _write_event(self) -> None:
        if self._event is None:
            return
        event_dir = self.root / self._event["event_id"]
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "event.json").write_text(
            json.dumps(self._event, indent=2), encoding="utf-8"
        )

    def _trace(
        self,
        timestamp: float,
        frame_num: int,
        bbox_count: int,
        operation: str | None,
        event_id: str | None,
        score: float,
        bbox: tuple[float, float, float, float] | None,
    ) -> None:
        payload = {
            "timestamp": round(timestamp, 6),
            "frame_num": frame_num,
            "camera": self.camera,
            "label": self.label,
            "state": self.state.value,
            "operation": operation,
            "event_id": event_id,
            "bbox_count": bbox_count,
            "score": round(score, 6),
            "bbox": bbox,
        }
        self._append(self.trace_path, payload)
        if event_id:
            self._append(self.root / event_id / "trace.jsonl", payload)

    def observe(
        self,
        frame_num: int,
        timestamp: float,
        detections: list[tuple[float, tuple[float, float, float, float]]],
    ) -> EventTransition | None:
        if not self.enabled:
            return None
        best = max(detections, key=lambda item: item[0], default=None)
        transition: EventTransition | None = None
        if best is not None:
            score, bbox = float(best[0]), self._bbox(best[1])
            self.last_score, self.last_bbox, self.clear_since = score, bbox, None
            if self.state is EventState.IDLE:
                self.state = EventState.PENDING
                self.candidate_since = timestamp
            if (
                self.state is EventState.PENDING
                and self.candidate_since is not None
                and timestamp - self.candidate_since >= self.confirm_seconds
            ):
                event_id = f"safety-{uuid.uuid4().hex[:24]}"
                self.active_event_id = event_id
                self.state = EventState.ACTIVE
                self.event_count += 1
                self._event = {
                    "event_id": event_id,
                    "camera": self.camera,
                    "label": self.label,
                    "state": "active",
                    "started_at": timestamp,
                    "ended_at": None,
                    "last_score": score,
                    "last_bbox": bbox,
                    "snapshot_count": 0,
                }
                self._write_event()
                transition = EventTransition("START", event_id, timestamp, frame_num, score, bbox)
            elif self.state is EventState.ACTIVE and self.active_event_id:
                if self._event is not None:
                    self._event["last_score"] = score
                    self._event["last_bbox"] = bbox
                transition = EventTransition(
                    "UPDATE", self.active_event_id, timestamp, frame_num, score, bbox
                )
        else:
            if self.state is EventState.PENDING:
                self.clear_since = self.clear_since or timestamp
                if timestamp - self.clear_since >= self.clear_seconds:
                    self.state = EventState.IDLE
                    self.candidate_since = None
                    self.clear_since = None
            elif self.state is EventState.ACTIVE and self.active_event_id:
                self.clear_since = self.clear_since or timestamp
                if timestamp - self.clear_since >= self.clear_seconds:
                    event_id = self.active_event_id
                    self.state = EventState.IDLE
                    self.active_event_id = None
                    self.candidate_since = None
                    self.clear_since = None
                    if self._event is not None:
                        self._event["state"] = "ended"
                        self._event["ended_at"] = timestamp
                        self._write_event()
                    transition = EventTransition(
                        "END", event_id, timestamp, frame_num, self.last_score, self.last_bbox
                    )
                    self._event = None

        event_id = self.active_event_id
        operation = transition.operation if transition else None
        self._trace(
            timestamp,
            frame_num,
            len(detections),
            operation,
            transition.event_id if transition else event_id,
            self.last_score if best is None else float(best[0]),
            self.last_bbox if best is None else self._bbox(best[1]),
        )
        return transition

    def save_snapshot(self, content: bytes, timestamp: float) -> Path | None:
        if not self.enabled or not self.active_event_id:
            return None
        event_id = self.active_event_id
        event_dir = self.root / event_id
        event_dir.mkdir(parents=True, exist_ok=True)
        count = int(self._event.get("snapshot_count", 0) if self._event else 0) + 1
        path = event_dir / f"snapshot-{int(timestamp * 1000)}-{count:04d}.jpg"
        path.write_bytes(content)
        if self._event is not None:
            self._event["snapshot_count"] = count
            self._write_event()
        return path

    def close(self) -> None:
        if self.active_event_id and self._event is not None:
            now = time.time()
            self._event["state"] = "ended"
            self._event["ended_at"] = now
            self._write_event()
            self._append(
                self.root / self.active_event_id / "trace.jsonl",
                {"timestamp": now, "operation": "END", "event_id": self.active_event_id},
            )
            self.active_event_id = None
