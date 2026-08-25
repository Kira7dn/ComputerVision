from __future__ import annotations

from typing import Any

import numpy as np

from domain.smoking_events import SmokingEpisodeStore, SmokingObservation, SmokingState


class RecordingEvidence:
    worker_epoch = "worker-test"

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.records: list[tuple[str, str, dict[str, Any]]] = []
        self.ends: list[tuple[str, dict[str, Any]]] = []

    def start_event(self, **payload: Any) -> str:
        self.starts.append(payload)
        return str(payload["event_id"])

    def record(self, event_id: str, operation: str, payload: dict[str, Any], **_: Any) -> bool:
        self.records.append((event_id, operation, payload))
        return True

    def finish_event(self, event_id: str, *, payload: dict[str, Any], **_: Any) -> None:
        self.ends.append((event_id, payload))


def _config(**lifecycle: float) -> dict[str, Any]:
    return {
        "input": {"camera": "camera_safety"},
        "smoking_behavior": {
            "enabled": True,
            "smoking_threshold": 0.60,
            "temporal": {
                "confirmation_hits": 2,
                "confirmation_window": 4,
                "minimum_duration_seconds": 0.4,
                "clear_negative_observations": 4,
            },
            "lifecycle": {
                "candidate_timeout_seconds": 3.0,
                "clearing_seconds": 3.0,
                "notification_min_duration_seconds": 3.0,
                "trace_interval_ms": 400,
                **lifecycle,
            },
        },
    }


def _observation(track_id: int, score: float, offset: float = 0.0) -> SmokingObservation:
    return SmokingObservation(
        track_id,
        score,
        (10.0 + offset, 10.0, 40.0 + offset, 80.0),
        (4.0 + offset, 0.0, 46.0 + offset, 94.0),
    )


def _observe(
    store: SmokingEpisodeStore,
    timestamp: float,
    observations: list[SmokingObservation],
    observed: set[int] | None = None,
    frame_number: int | None = None,
    invalid: set[int] | None = None,
):
    return store.observe(
        frame_num=frame_number if frame_number is not None else int(timestamp * 10),
        timestamp=timestamp,
        observations=observations,
        observed_track_ids=observed if observed is not None else {item.track_id for item in observations},
        invalid_crop_track_ids=invalid,
        frame=np.full((100, 100, 3), int(timestamp) % 255, dtype=np.uint8),
    )


def test_candidate_is_hidden_and_two_people_confirm_independently() -> None:
    evidence = RecordingEvidence()
    store = SmokingEpisodeStore(_config(), evidence)  # type: ignore[arg-type]

    assert _observe(store, 1.0, [_observation(7, 0.91), _observation(8, 0.82)]) == []
    assert store.visible_detections == []
    transitions = _observe(store, 1.4, [_observation(7, 0.80), _observation(8, 0.79)])

    assert [item.operation for item in transitions] == ["START", "START"]
    assert [item.person_track_id for item in transitions] == [7, 8]
    assert len(store.active_event_ids) == 2
    assert {item.confirmation_state for item in store.visible_detections} == {"CONFIRMED"}


def test_only_fresh_observations_advance_m_of_n_and_best_frame_is_exact() -> None:
    evidence = RecordingEvidence()
    store = SmokingEpisodeStore(_config(), evidence)  # type: ignore[arg-type]

    _observe(store, 1.0, [_observation(3, 0.72)], frame_number=10)
    _observe(store, 1.2, [], observed={3}, frame_number=11, invalid={3})
    assert _observe(store, 1.4, [_observation(3, 0.61)], frame_number=12)[0].operation == "START"

    start = evidence.starts[0]
    assert start["frame_number"] == 10
    assert start["score"] == 0.72
    assert start["metadata"]["best_frame_number"] == 10
    assert store.metrics()["invalid_crop_observations"] == 1


def test_negative_clearing_reacquires_same_event_then_closes_after_missing_grace() -> None:
    evidence = RecordingEvidence()
    store = SmokingEpisodeStore(_config(notification_min_duration_seconds=30.0), evidence)  # type: ignore[arg-type]
    _observe(store, 1.0, [_observation(5, 0.8)])
    start = _observe(store, 1.4, [_observation(5, 0.8)])[0]

    for index, timestamp in enumerate((1.8, 2.2, 2.6, 3.0), start=1):
        _observe(store, timestamp, [_observation(5, 0.2)], frame_number=20 + index)
    assert store.state == SmokingState.CLEARING
    _observe(store, 3.2, [_observation(5, 0.9)])
    assert store.state == SmokingState.CONFIRMED
    assert store.active_event_id == start.event_id
    assert store.metrics()["reacquired_episodes"] == 1

    _observe(store, 4.0, [], observed=set())
    ended = _observe(store, 7.1, [], observed=set())
    assert [item.operation for item in ended] == ["END"]
    assert ended[0].event_id == start.event_id
    assert store.state is None


def test_candidate_timeout_and_deterministic_next_episode_id() -> None:
    evidence = RecordingEvidence()
    store = SmokingEpisodeStore(_config(), evidence)  # type: ignore[arg-type]
    _observe(store, 1.0, [_observation(9, 0.7)])
    _observe(store, 4.1, [], observed=set())
    assert store.state is None

    _observe(store, 5.0, [_observation(9, 0.8)])
    started = _observe(store, 5.4, [_observation(9, 0.8)])[0]
    assert started.event_id == "smoking-worker-test-person-9-episode-0002"


def test_notification_is_delayed_persisted_and_emitted_once() -> None:
    evidence = RecordingEvidence()
    store = SmokingEpisodeStore(_config(), evidence)  # type: ignore[arg-type]
    _observe(store, 1.0, [_observation(2, 0.75)])
    assert [item.operation for item in _observe(store, 1.4, [_observation(2, 0.76)])] == ["START"]

    transitions = _observe(store, 4.0, [_observation(2, 0.77)])
    assert "NOTIFY" in [item.operation for item in transitions]
    _observe(store, 4.5, [_observation(2, 0.78)])
    assert [operation for _, operation, _ in evidence.records].count("NOTIFY") == 1
    assert store.metrics()["notification_count"] == 1


def test_shutdown_ends_all_confirmed_people() -> None:
    evidence = RecordingEvidence()
    store = SmokingEpisodeStore(_config(), evidence)  # type: ignore[arg-type]
    _observe(store, 1.0, [_observation(1, 0.8), _observation(2, 0.8)])
    _observe(store, 1.4, [_observation(1, 0.8), _observation(2, 0.8)])

    store.close()

    assert len(evidence.ends) == 2
    assert store.active_event_ids == []
