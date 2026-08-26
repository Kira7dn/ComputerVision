"""Bounded, frame-aligned scheduling for independent analysis functions."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from domain.contracts import AnalysisSample, FunctionResult


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reason: str | None = None
    age_seconds: float = 0.0


class FrameResultGate:
    """Reject stale or regressive results before they can mutate event state."""

    def __init__(self, max_age_seconds: float) -> None:
        self.max_age_seconds = max(0.001, float(max_age_seconds))
        self._lock = threading.Lock()
        self._last_order: dict[str, tuple[int, int]] = {}

    def evaluate(
        self,
        function: str,
        sample: AnalysisSample,
        finished_monotonic: float,
    ) -> GateDecision:
        age = max(0.0, finished_monotonic - sample.captured_monotonic)
        if age > self.max_age_seconds:
            return GateDecision(False, "stale", age)
        order = sample.key.ordering_value
        with self._lock:
            previous = self._last_order.get(function)
            if previous is not None and order <= previous:
                return GateDecision(False, "out_of_order", age)
            self._last_order[function] = order
        return GateDecision(True, age_seconds=age)


class AnalysisAdmissionGate:
    """Throttle the CPU conversion branch before pixels leave NVMM.

    The downstream executors still own their independent function cadences.
    This gate only limits how often a frame is allowed through the expensive
    NVMM-to-BGRx conversion to the fastest configured analysis cadence.
    """

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.001, float(interval_seconds))
        self._lock = threading.Lock()
        self._next_due = 0.0
        self.accepted = 0
        self.dropped = 0

    def accept(self, now: float) -> bool:
        with self._lock:
            # Select the source frame nearest to the fixed cadence deadline.
            # Advancing the deadline by a full slot still bounds admission to
            # one frame per interval when the source runs faster than analysis.
            tolerance = self.interval_seconds * 0.50
            if self._next_due and now + tolerance < self._next_due:
                self.dropped += 1
                return False
            if not self._next_due:
                self._next_due = now + self.interval_seconds
            else:
                elapsed = max(0.0, now - self._next_due)
                elapsed_slots = int(elapsed // self.interval_seconds)
                self._next_due += (elapsed_slots + 1) * self.interval_seconds
            self.accepted += 1
            return True

    def status(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "interval_seconds": self.interval_seconds,
                "planned_rate_hz": round(1.0 / self.interval_seconds, 3),
                "accepted": self.accepted,
                "dropped": self.dropped,
            }


class LatestSampleExecutor:
    """Single consumer with latest-only admission and observable drops."""

    def __init__(
        self,
        name: str,
        interval_seconds: float,
        processor: Callable[[AnalysisSample], Any],
        on_result: Callable[[str, AnalysisSample, Any, float, float], None],
        on_error: Callable[[str, Exception], None],
    ) -> None:
        self.name = name
        self.interval_seconds = max(0.001, float(interval_seconds))
        self._processor = processor
        self._on_result = on_result
        self._on_error = on_error
        self._queue: queue.Queue[AnalysisSample | None] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_due = 0.0
        self.enqueued = 0
        self.processed = 0
        self.dropped = 0
        self.errors = 0
        self.last_enqueued_frame: int | None = None
        self.last_processed_frame: int | None = None
        self.last_inference_seconds: float | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"analysis-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def is_due(self, now: float) -> bool:
        with self._lock:
            tolerance = self.interval_seconds * 0.50
            return not self._next_due or now + tolerance >= self._next_due

    def submit(self, sample: AnalysisSample, now: float) -> bool:
        with self._lock:
            tolerance = self.interval_seconds * 0.50
            if self._next_due and now + tolerance < self._next_due:
                return False
            if not self._next_due:
                self._next_due = now + self.interval_seconds
            else:
                elapsed = max(0.0, now - self._next_due)
                elapsed_slots = int(elapsed // self.interval_seconds)
                self._next_due += (elapsed_slots + 1) * self.interval_seconds
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(sample)
            except queue.Full:
                self.dropped += 1
                return False
        self.enqueued += 1
        self.last_enqueued_frame = sample.key.frame_number
        return True

    def stop(self, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
        thread.join(timeout=timeout)
        self._thread = None

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def status(self) -> dict[str, Any]:
        total = self.processed + self.dropped
        return {
            "interval_seconds": self.interval_seconds,
            "planned_rate_hz": round(1.0 / self.interval_seconds, 3),
            "queue_depth": self.queue_depth,
            "enqueued": self.enqueued,
            "processed": self.processed,
            "dropped": self.dropped,
            "drop_ratio": round(self.dropped / total, 6) if total else 0.0,
            "errors": self.errors,
            "last_enqueued_frame": self.last_enqueued_frame,
            "last_processed_frame": self.last_processed_frame,
            "last_inference_seconds": self.last_inference_seconds,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if sample is None:
                break
            started = time.monotonic()
            try:
                result = self._processor(sample)
                finished = time.monotonic()
                self.processed += 1
                self.last_processed_frame = sample.key.frame_number
                self.last_inference_seconds = max(0.0, finished - started)
                self._on_result(self.name, sample, result, started, finished)
            except Exception as exc:
                self.errors += 1
                self._on_error(self.name, exc)


def as_function_result(
    function: str,
    sample: AnalysisSample,
    detections: Any,
    started_monotonic: float,
    finished_monotonic: float,
    *,
    transitions: Any = (),
    model_revision: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FunctionResult:
    return FunctionResult(
        function=function,
        key=sample.key,
        detections=tuple(detections),
        transitions=tuple(transitions),
        started_monotonic=started_monotonic,
        finished_monotonic=finished_monotonic,
        model_revision=model_revision,
        metadata=dict(metadata or {}),
    )
