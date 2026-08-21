from __future__ import annotations

import threading
import time

import numpy as np

from application.analysis_scheduler import FrameResultGate, LatestSampleExecutor
from domain.contracts import AnalysisSample, FrameKey


def _sample(frame_number: int, *, captured: float | None = None) -> AnalysisSample:
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame.setflags(write=False)
    return AnalysisSample(
        key=FrameKey(
            run_id="run-1",
            camera_id="camera_safety",
            source_id=0,
            frame_number=frame_number,
            buffer_pts_ns=frame_number * 1_000_000,
        ),
        source_timestamp=1_700_000_000.0 + frame_number,
        captured_monotonic=time.monotonic() if captured is None else captured,
        frame=frame,
        persons=(),
    )


def test_result_gate_rejects_stale_duplicate_and_regressive_results() -> None:
    now = time.monotonic()
    gate = FrameResultGate(max_age_seconds=1.0)

    assert gate.evaluate("fire_smoke", _sample(10, captured=now), now + 0.2).accepted
    duplicate = gate.evaluate("fire_smoke", _sample(10, captured=now), now + 0.3)
    regressive = gate.evaluate("fire_smoke", _sample(9, captured=now), now + 0.3)
    stale = gate.evaluate("smoking_behavior", _sample(11, captured=now), now + 1.1)

    assert duplicate.reason == "out_of_order"
    assert regressive.reason == "out_of_order"
    assert stale.reason == "stale"


def test_latest_executor_drops_intermediate_sample_without_growing_queue() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    completed = threading.Event()
    processed: list[int] = []

    def processor(sample: AnalysisSample) -> int:
        if sample.key.frame_number == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        return sample.key.frame_number

    def on_result(name, sample, result, started, finished) -> None:
        processed.append(result)
        if result == 3:
            completed.set()

    executor = LatestSampleExecutor(
        "fire_smoke",
        0.001,
        processor,
        on_result,
        lambda _name, exc: (_ for _ in ()).throw(exc),
    )
    executor.start()
    try:
        assert executor.submit(_sample(1), 1.0)
        assert first_started.wait(timeout=2)
        assert executor.submit(_sample(2), 2.0)
        assert executor.submit(_sample(3), 3.0)
        assert executor.queue_depth == 1
        release_first.set()
        assert completed.wait(timeout=2)
    finally:
        release_first.set()
        executor.stop()

    assert processed == [1, 3]
    assert executor.dropped == 1


def test_fire_smoke_and_smoking_receive_same_frame_when_persons_are_empty() -> None:
    completed = threading.Event()
    seen: dict[str, tuple[int, int]] = {}
    executors: list[LatestSampleExecutor] = []

    def on_result(name, sample, result, started, finished) -> None:
        seen[name] = (sample.key.frame_number, len(sample.persons))
        if len(seen) == 2:
            completed.set()

    for name in ("fire_smoke", "smoking_behavior"):
        executor = LatestSampleExecutor(
            name,
            0.001,
            lambda sample: sample.key.frame_number,
            on_result,
            lambda _name, exc: (_ for _ in ()).throw(exc),
        )
        executor.start()
        executors.append(executor)
    try:
        sample = _sample(21)
        for executor in executors:
            assert executor.submit(sample, 1.0)
        assert completed.wait(timeout=2)
    finally:
        for executor in executors:
            executor.stop()

    assert seen == {
        "fire_smoke": (21, 0),
        "smoking_behavior": (21, 0),
    }
