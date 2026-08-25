from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from adapters.models.smoking_engine import SmokingBehaviorEngine
from adapters.models.smoking_object_detector import (
    SmokingObjectDetection,
    SmokingObjectDetector,
    _Model,
)
from domain.smoking_events import SmokingEpisodeStore, SmokingObservation


def _detector() -> SmokingObjectDetector:
    detector = SmokingObjectDetector(
        {"smoking_behavior": {"object_detection": {"enabled": False}}}
    )
    detector.enabled = True
    detector.confidence = 0.35
    detector.nms_iou = 0.50
    detector.person_match_iou = 0.10
    return detector


def test_tbox_yolo_output_decodes_only_canonical_positive_class() -> None:
    detector = _detector()
    model = _Model(
        "soham",
        (
            "Distracted",
            "Drinking",
            "Drowsy",
            "Eating",
            "PhoneUse",
            "SafeDriving",
            "Seatbelt",
            "Smoking",
        ),
        frozenset({"Smoking"}),
        SimpleNamespace(),
        "images",
    )
    # cx, cy, width, height followed by eight class scores.
    output = np.array(
        [[
            [50.0], [60.0], [20.0], [30.0],
            [0.05], [0.02], [0.01], [0.02], [0.03], [0.04], [0.05], [0.80],
        ]],
        dtype=np.float32,
    )
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    detections = detector._decode(model, output, frame, 1.0, 0.0, 0.0)

    assert len(detections) == 1
    assert detections[0].source == "soham"
    assert detections[0].label == "Smoking"
    assert detections[0].score == pytest.approx(0.8)
    assert detections[0].bbox == (40.0, 45.0, 60.0, 75.0)


def test_object_detection_is_spatially_assigned_to_one_person() -> None:
    detector = _detector()
    smoking = SmokingObjectDetection(
        "soham", "Smoking", 0.72, (20.0, 20.0, 25.0, 28.0)
    )

    assert detector.matches_person(smoking, (10.0, 10.0, 50.0, 90.0))
    assert not detector.matches_person(smoking, (60.0, 10.0, 95.0, 90.0))


def test_one_object_signal_is_not_counted_for_two_overlapping_people() -> None:
    engine = SmokingBehaviorEngine(
        {
            "smoking_behavior": {
                "enabled": False,
                "smoking_threshold": 0.60,
                "object_detection": {"enabled": False},
            }
        }
    )
    engine.enabled = True
    engine.session = object()
    engine._score = lambda _crop: 0.10  # type: ignore[method-assign]
    smoking = SmokingObjectDetection(
        "soham", "Smoking", 0.72, (35.0, 20.0, 40.0, 28.0)
    )
    engine.object_detector.enabled = True
    engine.object_detector.person_match_iou = 0.10
    engine.object_detector.process = lambda _frame: [smoking]  # type: ignore[method-assign]
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    batch = engine.process(
        frame,
        [
            (1, 10.0, 10.0, 50.0, 90.0),
            (2, 30.0, 10.0, 70.0, 90.0),
        ],
        1,
    )

    assert sum(bool(observation.positive) for observation in batch.observations) == 1
    positive = next(observation for observation in batch.observations if observation.positive)
    assert positive.track_id == 1


class _Evidence:
    worker_epoch = "worker"

    def __init__(self) -> None:
        self.starts = []

    def start_event(self, **payload):
        self.starts.append(payload)
        return payload["event_id"]

    def record(self, *_args, **_kwargs):
        return True

    def finish_event(self, *_args, **_kwargs):
        return None


def test_tbox_positive_signal_advances_episode_below_classifier_threshold() -> None:
    config = {
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
            },
        },
    }
    evidence = _Evidence()
    store = SmokingEpisodeStore(config, evidence)  # type: ignore[arg-type]
    observation = SmokingObservation(
        7,
        0.60,
        (10.0, 10.0, 50.0, 90.0),
        (2.0, 0.0, 58.0, 100.0),
        positive=True,
        classifier_score=0.18,
        object_score=0.72,
        signal_sources=("tbox:soham:Smoking",),
    )

    assert store.observe(
        frame_num=1,
        timestamp=1.0,
        observations=[observation],
        observed_track_ids={7},
        frame=None,
    ) == []
    transitions = store.observe(
        frame_num=2,
        timestamp=1.4,
        observations=[observation],
        observed_track_ids={7},
        frame=None,
    )

    assert [transition.operation for transition in transitions] == ["START"]
    assert evidence.starts[0]["metadata"]["best_signal_sources"] == [
        "tbox:soham:Smoking"
    ]
