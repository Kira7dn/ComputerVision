"""Canonical, deduplicated evidence storage for one DeepStream run."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unknown"


def run_directory(config: dict[str, Any], run_id: str) -> Path:
    evidence = config.get("evidence", {}) or {}
    root = Path(str(evidence.get("directory", ".tmp/deepstream-safety")))
    prefix = str(evidence.get("prefix", "snapshots-acceptance"))
    return root / f"{prefix}-{_slug(run_id)}"


def write_manifest(config: dict[str, Any], run_id: str, target: Path) -> None:
    cameras = []
    for camera in config.get("cameras", []) or []:
        cameras.append(
            {
                "id": str(camera.get("id")),
                "functions": dict(camera.get("functions", {}) or {}),
                "source": str((camera.get("source", {}) or {}).get("url", "")),
            }
        )
    if not cameras:
        cameras = [
            {
                "id": str((config.get("input", {}) or {}).get("camera", "camera")),
                "functions": dict(config.get("functions", {}) or {}),
                "source": str((config.get("input", {}) or {}).get("rtsp_url", "")),
            }
        ]
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": time.time(),
        "cameras": cameras,
        "dedupe": {"index": "index.sqlite3", "key": "idempotency_key"},
    }
    temporary = target / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(target / "manifest.json")


class EvidenceStore:
    """Own all event JSON, JSONL traces, and event evidence images."""

    def __init__(self, config: dict[str, Any], run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        runtime = config.get("runtime", {}) or {}
        self.worker_epoch = str(runtime.get("worker_epoch", "worker-0"))
        self.camera_id = str((config.get("input", {}) or {}).get("camera", "camera"))
        self.root = run_directory(config, run_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.image_interval = max(
            0.1,
            float(
                (config.get("evidence", {}) or {}).get(
                    "snapshot_interval_ms",
                    (config.get("snapshots", {}) or {}).get("min_interval_ms", 1000),
                )
            )
            / 1000.0,
        )
        self._events: dict[str, dict[str, Any]] = {}
        self._last_image_at: dict[str, float] = {}
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            str(self.root / "index.sqlite3"),
            check_same_thread=False,
            timeout=30.0,
        )
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "idempotency_key TEXT PRIMARY KEY, path TEXT NOT NULL, "
            "kind TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        self.db.commit()
        manifest = self.root / "manifest.json"
        if not manifest.exists():
            write_manifest(config, run_id, self.root)

    def _claim(self, key: str, path: Path, kind: str) -> bool:
        with self._lock:
            try:
                self.db.execute(
                    "INSERT INTO records(idempotency_key,path,kind,created_at) VALUES(?,?,?,?)",
                    (key, str(path.relative_to(self.root)), kind, time.time()),
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                self.db.rollback()
                return False

    def _event_dir(self, event: dict[str, Any]) -> Path:
        # Keep the path immutable for the whole lifecycle. Classification is
        # mutable event metadata; moving directories made START journal rows
        # stale and was unreliable on the WSL /mnt mount.
        return (
            self.root
            / _slug(str(event["camera_id"]))
            / _slug(str(event["function"]))
            / _slug(str(event["event_id"]))
        )

    def _write_event(self, event: dict[str, Any]) -> None:
        event_dir = self._event_dir(event)
        event_dir.mkdir(parents=True, exist_ok=True)
        temporary = event_dir / "event.json.tmp"
        temporary.write_text(json.dumps(event, indent=2), encoding="utf-8")
        temporary.replace(event_dir / "event.json")

    def _append_event_index(self, event: dict[str, Any], record_type: str) -> None:
        path = self.root / "events.jsonl"
        key = f"{self.run_id}|{self.worker_epoch}|{event['camera_id']}|{event['function']}|{event['event_id']}|index|{record_type}"
        if not self._claim(key, path, "event-index"):
            return
        summary = {
            "record_type": record_type,
            "event_id": event["event_id"],
            "run_id": self.run_id,
            "worker_epoch": self.worker_epoch,
            "camera_id": event["camera_id"],
            "function": event["function"],
            "classification": event["classification"],
            "person_track_id": event.get("person_track_id"),
            "identity": event.get("identity"),
            "status": event["status"],
            "started_at": event["started_at"],
            "ended_at": event.get("ended_at"),
            "event_path": str(self._event_dir(event).relative_to(self.root)),
            "idempotency_key": key,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(summary, separators=(",", ":")) + "\n")

    @staticmethod
    def _annotation_label(event: dict[str, Any], payload: dict[str, Any], score: float | None) -> str:
        label = str(payload.get("label") or event.get("classification") or event.get("function"))
        if event.get("function") == "fire_smoke" and label == "smoke":
            label = "smoke area"
        if event.get("function") == "smoking_behavior":
            track_id = event.get("person_track_id")
            prefix = f"person #{track_id} | " if track_id is not None else "person | "
            return f"{prefix}{label.upper()} {score * 100:.0f}%" if score is not None else f"{prefix}{label.upper()}"
        if score is not None:
            return f"{label.upper()} {score * 100:.0f}%"
        return label.upper()

    @staticmethod
    def _annotation_color(event: dict[str, Any]) -> tuple[int, int, int]:
        if event.get("function") == "smoking_behavior":
            return (0, 0, 255)
        if str(event.get("classification")) == "fire":
            return (0, 140, 255)
        if str(event.get("classification")) == "smoke":
            # BGR cyan: high contrast against the dark camera and pale smoke.
            return (255, 255, 0)
        return (0, 220, 0)

    def start_event(
        self,
        *,
        event_id: str | None,
        function: str,
        classification: str,
        camera_id: str | None = None,
        person_track_id: int | None = None,
        pending: bool = False,
        metadata: dict[str, Any] | None = None,
        frame: np.ndarray | None = None,
        frame_number: int | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        score: float | None = None,
    ) -> str:
        event_id = event_id or f"evt-{uuid.uuid4().hex[:24]}"
        event = {
            "event_id": event_id,
            "run_id": self.run_id,
            "worker_epoch": self.worker_epoch,
            "camera_id": camera_id or self.camera_id,
            "function": function,
            "classification": "pending" if pending else classification,
            "status": "active",
            "person_track_id": person_track_id,
            "identity": None,
            "started_at": time.time(),
            "ended_at": None,
            "last_score": 0.0,
            "last_bbox": None,
            "snapshot_count": 0,
            **(metadata or {}),
        }
        self._events[event_id] = event
        self._write_event(event)
        self.record(
            event_id,
            "START",
            {
                "status": "active",
                "person_track_id": person_track_id,
                **(metadata or {}),
            },
            frame=frame,
            frame_number=frame_number,
            bbox=bbox,
            score=score,
            force_image=frame is not None,
        )
        self._append_event_index(event, "START")
        return event_id

    def record(
        self,
        event_id: str,
        record_type: str,
        payload: dict[str, Any],
        *,
        frame: np.ndarray | None = None,
        frame_number: int | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        score: float | None = None,
        force_image: bool = False,
    ) -> bool:
        event = self._events.get(event_id)
        if event is None:
            return False
        frame_key = "none" if frame_number is None else str(frame_number)
        key = f"{self.run_id}|{self.worker_epoch}|{event['camera_id']}|{event['function']}|{event_id}|{record_type}|{frame_key}"
        trace_path = self._event_dir(event) / "trace.jsonl"
        if not self._claim(key, trace_path, "trace"):
            return False

        evidence: list[str] = []
        now = time.time()
        should_image = frame is not None and (
            force_image
            or record_type in {"START", "END"}
            or now - self._last_image_at.get(event_id, 0.0) >= self.image_interval
        )
        if should_image:
            event_dir = self._event_dir(event)
            image_dir = event_dir / "snapshots"
            image_dir.mkdir(parents=True, exist_ok=True)
            sequence = int(event.get("snapshot_count", 0)) + 1
            stem = f"{record_type.lower()}-{sequence:04d}"
            full_path = image_dir / f"{stem}-full.jpg"
            annotated_path = image_dir / f"{stem}-annotated.jpg"
            roi_path = image_dir / f"{stem}-roi.jpg"
            image_key = f"{key}|image"
            if self._claim(image_key, full_path, "image"):
                if cv2.imwrite(str(full_path), frame):
                    evidence.append(str(full_path.relative_to(self.root)))
                    if bbox is not None:
                        annotated = frame.copy()
                        height, width = annotated.shape[:2]
                        left, top, right, bottom = [
                            int(max(0, value)) for value in bbox
                        ]
                        left = min(left, max(0, width - 1))
                        right = min(max(left + 1, right), width)
                        top = min(top, max(0, height - 1))
                        bottom = min(max(top + 1, bottom), height)
                        color = self._annotation_color(event)
                        cv2.rectangle(annotated, (left, top), (right, bottom), color, 3)
                        text = self._annotation_label(event, payload, score)
                        text_y = max(24, top - 8)
                        cv2.putText(
                            annotated,
                            text,
                            (left, text_y),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.75,
                            color,
                            2,
                            cv2.LINE_AA,
                        )
                        annotated_key = f"{key}|annotated-image"
                        if self._claim(annotated_key, annotated_path, "annotated-image"):
                            if cv2.imwrite(str(annotated_path), annotated):
                                evidence.append(str(annotated_path.relative_to(self.root)))
                    if bbox is not None:
                        left, top, right, bottom = [int(max(0, value)) for value in bbox]
                        crop = frame[top:bottom, left:right]
                        if crop.size and cv2.imwrite(str(roi_path), crop):
                            evidence.append(str(roi_path.relative_to(self.root)))
                    event["snapshot_count"] = sequence
                    self._last_image_at[event_id] = now

        trace = {
            "record_type": record_type,
            "event_id": event_id,
            "run_id": self.run_id,
            "camera_id": event["camera_id"],
            "function": event["function"],
            "classification": event["classification"],
            "timestamp": now,
            "frame": frame_number,
            "score": score,
            "bbox": list(bbox) if bbox is not None else None,
            "evidence": evidence,
            "idempotency_key": key,
            **payload,
        }
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(trace, separators=(",", ":")) + "\n")
        if score is not None:
            event["last_score"] = score
        if bbox is not None:
            event["last_bbox"] = list(bbox)
        for field in ("person_bbox", "model_roi_bbox", "bbox_semantics", "label"):
            if field in payload:
                event[field] = payload[field]
        self._write_event(event)
        return True

    def finish_event(
        self,
        event_id: str,
        *,
        classification: str | None = None,
        identity: str | None = None,
        payload: dict[str, Any] | None = None,
        frame: np.ndarray | None = None,
        frame_number: int | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        score: float | None = None,
    ) -> None:
        event = self._events.get(event_id)
        if event is None:
            return
        event["status"] = "ended"
        event["ended_at"] = time.time()
        if identity is not None:
            event["identity"] = identity
        final_classification = classification or str(event["classification"])
        end_payload = dict(payload or {})
        end_payload["classification"] = final_classification
        self.record(
            event_id,
            "END",
            end_payload,
            frame=frame,
            frame_number=frame_number,
            bbox=bbox,
            score=score,
            force_image=True,
        )
        if classification and event["classification"] != classification:
            event["classification"] = classification
        self._write_event(event)
        self._append_event_index(event, "END")

    def close(self) -> None:
        self.db.close()
