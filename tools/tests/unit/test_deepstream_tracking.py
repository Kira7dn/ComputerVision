import numpy as np

from deepstream_safety.tracking import (
    frigate_track_distance,
    opposite_frame_edge_transition,
)


def test_edge_spanning_person_box_is_not_an_opposite_edge_jump() -> None:
    previous = np.array([605.4, 21.2, 1847.5, 1070.0], dtype=np.float32)
    current = np.array([600.0, 18.0, 1850.0, 1072.0], dtype=np.float32)

    assert not opposite_frame_edge_transition(previous, current, 1920, 1080)
    assert frigate_track_distance(current, previous) < 0.1


def test_tracker_still_rejects_a_true_opposite_edge_jump() -> None:
    previous = np.array([0.0, 300.0, 180.0, 900.0], dtype=np.float32)
    current = np.array([1740.0, 300.0, 1920.0, 900.0], dtype=np.float32)

    assert opposite_frame_edge_transition(previous, current, 1920, 1080)
