"""Shared bounded YOLO inference scheduler for all configured channels."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class InferenceJob:
    channel: int
    frame: Any
    sequence: int
    received_at: float
    callback: Callable[[InferenceJob], None]


class InferenceScheduler:
    """One model instance, bounded latest-frame jobs, deterministic shutdown."""

    def __init__(self, model_path: Path, *, max_channels: int = 2):
        self.model_path = Path(model_path)
        self.jobs: queue.Queue[InferenceJob] = queue.Queue(maxsize=max_channels)
        self.slots: dict[int, InferenceJob] = {}
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.model = None
        self.closed = False
        self.thread: threading.Thread | None = None

    def load(self):
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
        from ultralytics import YOLO

        if self.model_path.suffix != '.engine':
            raise RuntimeError('production ADAS requires a TensorRT .engine model')
        try:
            import tensorrt  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(f'TensorRT runtime unavailable: {exc}') from exc
        if not self.model_path.is_file():
            raise RuntimeError(f'TensorRT engine not found: {self.model_path}')
        self.model = YOLO(str(self.model_path), task='detect')
        self.thread = threading.Thread(target=self._run, name='adas-inference', daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while True:
            job = self.take()
            if job is None:
                if self.closed:
                    return
                continue
            try:
                job.callback(job)
            except Exception:
                # The channel owns error reporting; scheduler must remain alive.
                continue

    def submit_latest(self, job: InferenceJob) -> None:
        with self.condition:
            if self.closed:
                return
            self.slots[job.channel] = job
            self.condition.notify()

    def take(self, timeout: float = 0.5) -> InferenceJob | None:
        with self.condition:
            if not self.slots and not self.closed:
                self.condition.wait(timeout)
            if self.closed or not self.slots:
                return None
            channel = min(self.slots)
            return self.slots.pop(channel)

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=2)


_shared: dict[str, InferenceScheduler] = {}
_shared_lock = threading.Lock()


def get_shared_scheduler(model_path: Path, *, max_channels: int = 2) -> InferenceScheduler:
    key = str(Path(model_path).resolve())
    with _shared_lock:
        scheduler = _shared.get(key)
        if scheduler is None or scheduler.closed:
            scheduler = InferenceScheduler(Path(model_path), max_channels=max_channels).load()
            _shared[key] = scheduler
        return scheduler
