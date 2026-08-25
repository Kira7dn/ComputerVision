from __future__ import annotations

from ls_vision.domain.front_assistance import (
    FrontPerception,
    FrontReadiness,
    VisionAlertPolicy,
)


def _perception(
    frame: int,
    *,
    left: bool = False,
    right: bool = False,
    fcw: bool = False,
    epoch: str = "epoch",
    valid: bool = True,
) -> FrontPerception:
    lanes = (
        ((0.0, -3.0, 1.2),),
        ((0.0, -1.0 if left else -2.0, 1.2),),
        ((0.0, 1.0 if right else 2.0, 1.2),),
        ((0.0, 3.0, 1.2),),
    )
    desire = (0.0, 0.2 if left else 0.0, 0.2 if right else 0.0)
    return FrontPerception(
        epoch,
        frame,
        frame / 20.0,
        valid,
        FrontReadiness.READY if valid else FrontReadiness.NOT_READY,
        () if valid else ("model_invalid",),
        lanes,
        (0.0, 0.8, 0.8, 0.0),
        (),
        (),
        (),
        desire,
        fcw,
        (),
        (),
        "CUDAExecutionProvider",
        10.0,
        "model",
        "calibration",
    )


def test_ldw_requires_window_and_closes_once() -> None:
    policy = VisionAlertPolicy()
    transitions = []
    for frame in range(1, 6):
        transitions.extend(policy.observe(_perception(frame, left=frame in {1, 3, 5})))
    assert [(item.operation, item.label) for item in transitions] == [
        ("START", "vision_ldw_left")
    ]
    for frame in range(6, 11):
        transitions.extend(policy.observe(_perception(frame)))
    assert [(item.operation, item.label) for item in transitions][-1] == (
        "END",
        "vision_ldw_left",
    )


def test_fcw_starts_immediately_and_invalid_state_clears() -> None:
    policy = VisionAlertPolicy(fcw_clear_observations=2)
    started = policy.observe(_perception(1, fcw=True))
    assert started[0].label == "vision_fcw"
    assert policy.observe(_perception(2, valid=False)) == []
    ended = policy.observe(_perception(3, valid=False))
    assert ended[0].operation == "END"


def test_epoch_change_never_carries_active_alert() -> None:
    policy = VisionAlertPolicy(fcw_clear_observations=2)
    policy.observe(_perception(1, fcw=True, epoch="old"))
    assert policy.active_labels == ("vision_fcw",)
    assert policy.observe(_perception(1, epoch="new")) == []
    assert policy.active_labels == ()
