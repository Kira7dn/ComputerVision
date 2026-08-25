from pathlib import Path

import numpy as np
import pytest

from ls_vision.bootstrap.config import load_raw_config, resolve_camera_config
from ls_vision.domain.tracking import (
    PersonConfirmation,
    intersection_over_candidate,
    opposite_frame_edge_transition,
    track_distance,
)

ROOT = Path(__file__).parents[4]


def test_edge_spanning_person_box_is_not_an_opposite_edge_jump() -> None:
    previous = np.array([605.4, 21.2, 1847.5, 1070.0], dtype=np.float32)
    current = np.array([600.0, 18.0, 1850.0, 1072.0], dtype=np.float32)

    assert not opposite_frame_edge_transition(previous, current, 1920, 1080)
    assert track_distance(current, previous) < 0.1


def test_tracker_still_rejects_a_true_opposite_edge_jump() -> None:
    previous = np.array([0.0, 300.0, 180.0, 900.0], dtype=np.float32)
    current = np.array([1740.0, 300.0, 1920.0, 900.0], dtype=np.float32)

    assert opposite_frame_edge_transition(previous, current, 1920, 1080)


def test_person_confirmation_requires_two_hits_in_four_frames() -> None:
    confirmation = PersonConfirmation(required_hits=2, window_frames=4)

    assert not confirmation.observe(10)
    assert confirmation.observe(12)
    assert confirmation.confirmed


def test_person_confirmation_drops_hits_outside_the_window() -> None:
    confirmation = PersonConfirmation(required_hits=2, window_frames=4)

    assert not confirmation.observe(10)
    assert not confirmation.observe(14)
    assert list(confirmation.hit_frames) == [14]


def test_person_confirmation_stays_confirmed_after_later_missed_frames() -> None:
    confirmation = PersonConfirmation(required_hits=2, window_frames=4)

    confirmation.observe(10)
    confirmation.observe(11)

    assert confirmation.observe(20)


def test_person_confirmation_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="window_frames"):
        PersonConfirmation(required_hits=3, window_frames=2)


def test_person_confirmation_config_is_resolved_per_camera() -> None:
    config = resolve_camera_config(
        load_raw_config(ROOT / "apps" / "ls-vision" / "config" / "production.yaml"),
        "DMS",
    )

    assert config["person"]["confidence"] == 0.05
    assert config["person"]["tracking"]["confirmation_hits"] == 2
    assert config["person"]["tracking"]["confirmation_window"] == 4
    assert "fire_smoke_exclusion_overlap_ratio" not in config["person"]["tracking"]


def test_fire_smoke_overlap_uses_candidate_area() -> None:
    person = np.array([0.0, 0.0, 100.0, 100.0], dtype=np.float32)
    fire = np.array([25.0, 25.0, 75.0, 75.0], dtype=np.float32)

    assert intersection_over_candidate(person, fire) == pytest.approx(0.25)


def test_fire_smoke_overlap_rejects_non_overlapping_boxes() -> None:
    person = np.array([0.0, 0.0, 100.0, 100.0], dtype=np.float32)
    fire = np.array([120.0, 120.0, 180.0, 180.0], dtype=np.float32)

    assert intersection_over_candidate(person, fire) == 0.0
