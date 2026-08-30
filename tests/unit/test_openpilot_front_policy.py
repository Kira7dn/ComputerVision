from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from domain.front_assistance import (
    FrontLead,
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
    lead_ttc: bool = False,
    edge_left: bool = False,
    edge_right: bool = False,
    geometry_roll_deg: float = 0.0,
    timestamp: float | None = None,
    low_confidence: bool = False,
) -> FrontPerception:
    lanes = (
        ((0.0, -3.0, 1.2),),
        ((0.0, -1.0 if left else -2.0, 1.2),),
        ((0.0, 1.0 if right else 2.0, 1.2),),
        ((0.0, 3.0, 1.2),),
    )
    # openpilot Desire enum: laneChangeLeft=3, laneChangeRight=4.
    desire = (0.0, 0.0, 0.0, 0.2 if left else 0.0, 0.2 if right else 0.0)
    distances = tuple(float(index) for index in range(33))
    return FrontPerception(
        epoch,
        frame,
        frame / 20.0 if timestamp is None else timestamp,
        valid,
        FrontReadiness.READY if valid else FrontReadiness.NOT_READY,
        () if valid else ("model_invalid",),
        lanes,
        (0.0, 0.2, 0.2, 0.0) if low_confidence else (0.0, 0.8, 0.8, 0.0),
        (
            tuple((x, -1.0 if edge_left else -3.0, 0.0) for x in distances),
            tuple((x, 1.0 if edge_right else 3.0, 0.0) for x in distances),
        ),
        tuple((x, 0.0, 0.0) for x in distances),
        (
            FrontLead(
                probability=0.4 if low_confidence else 0.9,
                x=(6.0,),
                y=(0.0,),
                velocity=(-3.0 if lead_ttc else -1.0,),
                acceleration=(0.0,),
            ),
        ),
        desire,
        fcw,
        (),
        (),
        "CUDAExecutionProvider",
        10.0,
        "model",
        "calibration",
        road_edge_stds=tuple(
            tuple((1.0 if low_confidence else 0.2, 0.1) for _ in distances)
            for _ in range(2)
        ),
        wide_from_device_euler=(math.radians(geometry_roll_deg), 0.0, 0.0),
        road_transform=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
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
    policy = VisionAlertPolicy(
        config={"fcw_confirmation_hits": 1, "fcw_confirmation_window": 1},
        fcw_clear_observations=2,
    )
    started = policy.observe(_perception(1, fcw=True))
    assert started[0].label == "vision_fcw"
    assert policy.observe(_perception(2, valid=False)) == []
    ended = policy.observe(_perception(3, valid=False))
    assert ended[0].operation == "END"


def test_high_sensitivity_fcw_uses_brake_probability_with_hysteresis() -> None:
    policy = VisionAlertPolicy(
        config={
            "fcw_brake_probability": 0.02,
            "fcw_clear_probability": 0.01,
            "fcw_clear_observations": 2,
            "fcw_confirmation_hits": 1,
            "fcw_confirmation_window": 1,
        }
    )
    started = policy.observe(
        replace(_perception(1), hard_brake_3_probs=(0.011, 0.021))
    )
    assert [(item.operation, item.label) for item in started] == [
        ("START", "vision_fcw")
    ]
    assert started[0].metadata["fcw_brake_probability"] == 0.021
    assert policy.observe(
        replace(_perception(2), hard_brake_3_probs=(0.009, 0.012))
    ) == []
    assert policy.observe(
        replace(_perception(3), hard_brake_3_probs=(0.004, 0.009))
    ) == []
    ended = policy.observe(
        replace(_perception(4), hard_brake_3_probs=(0.003, 0.008))
    )
    assert [(item.operation, item.label) for item in ended] == [
        ("END", "vision_fcw")
    ]


def test_fcw_ignores_single_probability_spike() -> None:
    policy = VisionAlertPolicy(
        config={
            "fcw_brake_probability": 0.09,
            "fcw_confirmation_hits": 3,
            "fcw_confirmation_window": 5,
        }
    )
    assert policy.observe(replace(_perception(1), hard_brake_3_probs=(0.1, 0.1))) == []
    for frame in (2, 3, 4, 5):
        assert policy.observe(replace(_perception(frame), hard_brake_3_probs=(0.0, 0.0))) == []
    assert policy.observe(replace(_perception(6), hard_brake_3_probs=(0.1, 0.1))) == []
    assert policy.observe(replace(_perception(7), hard_brake_3_probs=(0.1, 0.1))) == []
    started = policy.observe(replace(_perception(8), hard_brake_3_probs=(0.1, 0.1)))
    assert [(item.operation, item.label) for item in started] == [("START", "vision_fcw")]


def test_epoch_change_never_carries_active_alert() -> None:
    policy = VisionAlertPolicy(
        config={"fcw_confirmation_hits": 1, "fcw_confirmation_window": 1},
        fcw_clear_observations=2,
    )
    policy.observe(_perception(1, fcw=True, epoch="old"))
    assert policy.active_labels == ("vision_fcw",)
    ended = policy.observe(_perception(1, epoch="new"))
    assert [(item.operation, item.label) for item in ended] == [
        ("END", "vision_fcw")
    ]
    assert policy.active_labels == ()


def test_lead_ttc_requires_three_of_five_and_ten_clear_frames() -> None:
    policy = VisionAlertPolicy()
    transitions = []
    for frame in range(1, 6):
        transitions.extend(
            policy.observe(_perception(frame, lead_ttc=frame in {1, 3, 5}))
        )
    assert [(item.operation, item.label) for item in transitions] == [
        ("START", "vision_lead_ttc")
    ]
    start = transitions[0]
    assert start.metadata["ttc_seconds"] == 1.4933333333333334
    for frame in range(6, 16):
        transitions.extend(policy.observe(_perception(frame)))
    assert [(item.operation, item.label) for item in transitions][-1] == (
        "END",
        "vision_lead_ttc",
    )


def test_lead_ttc_uses_ego_minus_lead_velocity() -> None:
    policy = VisionAlertPolicy(
        config={
            "lead_confirmation_hits": 1,
            "lead_confirmation_window": 1,
        }
    )
    perception = replace(
        _perception(1),
        leads=(
            FrontLead(
                probability=0.9,
                x=(6.0,),
                y=(0.0,),
                velocity=(5.0,),
                acceleration=(0.0,),
            ),
        ),
        plan_velocity=((8.0, 0.0, 0.0),),
    )
    started = policy.observe(perception)
    lead = next(item for item in started if item.label == "vision_lead_ttc")
    assert lead.metadata["ego_velocity_mps"] == 8.0
    assert lead.metadata["lead_velocity_mps"] == 5.0
    assert lead.metadata["closing_speed_mps"] == 3.0
    assert lead.metadata["ttc_seconds"] == 1.4933333333333334


def test_front_telemetry_features_are_scalar_and_physically_meaningful() -> None:
    policy = VisionAlertPolicy()
    perception = replace(
        _perception(1),
        source_timestamp=1_787_812_140.125,
        plan_velocity=((8.0, 0.0, 0.0),),
        leads=(
            FrontLead(
                probability=0.9,
                x=(12.0,),
                y=(0.0,),
                velocity=(5.0,),
                acceleration=(-0.5,),
            ),
        ),
        hard_brake_3_probs=(0.01, 0.08, 0.03),
    )

    features = policy.telemetry_features(perception)

    assert features == {
        "source_timestamp_ms": 1_787_812_140_125.0,
        "ready": True,
        "lead_probability": 0.9,
        "lead_distance_m": 10.48,
        "lead_velocity_mps": 5.0,
        "closing_speed_mps": 3.0,
        "ttc_s": 3.4933333333333336,
        "lane_left_distance_m": 2.0,
        "lane_right_distance_m": 2.0,
        "lane_left_probability": 0.8,
        "lane_right_probability": 0.8,
        "road_edge_left_clearance_m": 2.1,
        "road_edge_right_clearance_m": 2.1,
        "hard_brake_probability": 0.08,
    }
    assert all(
        value is None or isinstance(value, bool | int | float)
        for value in features.values()
    )


def test_high_sensitivity_ldw_can_confirm_in_one_frame() -> None:
    policy = VisionAlertPolicy(
        config={
            "ldw_lane_close_m": 1.5,
            "ldw_lane_probability": 0.05,
            "ldw_desire_probability": 0.005,
            "ldw_confirmation_hits": 1,
            "ldw_confirmation_window": 1,
            "ldw_clear_observations": 2,
        }
    )
    started = policy.observe(_perception(1, left=True))
    assert [(item.operation, item.label) for item in started] == [
        ("START", "vision_ldw_left")
    ]
    assert policy.observe(_perception(2)) == []
    ended = policy.observe(_perception(3))
    assert [(item.operation, item.label) for item in ended] == [
        ("END", "vision_ldw_left")
    ]


def test_ldw_uses_lane_change_desire_enum_not_turn_desire() -> None:
    policy = VisionAlertPolicy(
        config={"ldw_confirmation_hits": 1, "ldw_confirmation_window": 1}
    )
    turning = replace(_perception(1, left=True), desire_prediction=(0.0, 0.9, 0.0, 0.0, 0.0))
    assert policy.observe(turning) == []
    lane_change = replace(
        _perception(2, left=True),
        desire_prediction=(0.0, 0.0, 0.0, 0.9, 0.0),
    )
    assert [(item.operation, item.label) for item in policy.observe(lane_change)] == [
        ("START", "vision_ldw_left")
    ]


def test_both_road_edges_have_independent_hysteresis() -> None:
    policy = VisionAlertPolicy()
    transitions = []
    for frame in range(1, 6):
        transitions.extend(
            policy.observe(
                _perception(
                    frame,
                    edge_left=frame in {1, 3, 5},
                    edge_right=frame in {2, 4, 5},
                )
            )
        )
    assert {(item.operation, item.label) for item in transitions} == {
        ("START", "vision_road_edge_left"),
        ("START", "vision_road_edge_right"),
    }
    for frame in range(6, 16):
        transitions.extend(policy.observe(_perception(frame)))
    assert {(item.operation, item.label) for item in transitions[-2:]} == {
        ("END", "vision_road_edge_left"),
        ("END", "vision_road_edge_right"),
    }


def test_unusable_edge_uncertainty_eventually_clears() -> None:
    policy = VisionAlertPolicy(
        config={
            "edge_confirmation_hits": 1,
            "edge_confirmation_window": 1,
            "edge_clear_observations": 2,
        }
    )
    assert policy.observe(_perception(1, edge_left=True))[0].operation == "START"
    bad_stds = tuple(tuple((9.0, 0.1) for _ in range(33)) for _ in range(2))
    ended = []
    for frame in range(2, 4):
        ended.extend(policy.observe(replace(_perception(frame), road_edge_stds=bad_stds)))
    assert [(item.operation, item.label) for item in ended] == [
        ("END", "vision_road_edge_left")
    ]


def test_geometry_drift_learns_baseline_and_clears_below_sixty_percent() -> None:
    policy = VisionAlertPolicy(
        config={
            "geometry_baseline_frames": 3,
            "geometry_trigger_hits": 3,
            "geometry_trigger_window": 5,
            "geometry_clear_observations": 4,
        }
    )
    transitions = []
    for frame in range(1, 4):
        transitions.extend(policy.observe(_perception(frame)))
    for frame in range(4, 9):
        transitions.extend(
            policy.observe(
                _perception(frame, geometry_roll_deg=3.0 if frame in {4, 6, 8} else 0.0)
            )
        )
    assert transitions[0].label == "vision_geometry_drift"
    assert transitions[0].metadata["experimental_advisory"] is True
    for frame in range(9, 13):
        transitions.extend(policy.observe(_perception(frame, geometry_roll_deg=1.0)))
    assert transitions[-1].operation == "END"
    assert transitions[-1].label == "vision_geometry_drift"


def test_banner_priority_and_timestamp_discontinuity_reset_all_state() -> None:
    policy = VisionAlertPolicy(
        config={"fcw_confirmation_hits": 1, "fcw_confirmation_window": 1}
    )
    for frame in range(1, 6):
        policy.observe(_perception(frame, lead_ttc=True))
    assert policy.banner_label == "vision_lead_ttc"
    policy.observe(_perception(6, fcw=True))
    assert policy.banner_label == "vision_fcw"
    reset_transitions = policy.observe(_perception(7, timestamp=10.0))
    assert {(item.operation, item.label) for item in reset_transitions} == {
        ("END", "vision_fcw"),
        ("END", "vision_lead_ttc"),
    }
    assert policy.active_labels == ()


def test_mock_cycle_reset_closes_active_alert_and_restarts_state() -> None:
    policy = VisionAlertPolicy(
        config={
            "fcw_clear_observations": 20,
            "fcw_confirmation_hits": 1,
            "fcw_confirmation_window": 1,
        }
    )
    first = replace(_perception(1, fcw=True), diagnostics={"mock_cycle_index": 0})
    assert policy.observe(first)[0].operation == "START"
    second = replace(_perception(2), diagnostics={"mock_cycle_index": 1})
    ended = policy.observe(second)
    assert [(item.operation, item.label) for item in ended] == [
        ("END", "vision_fcw")
    ]
    assert ended[0].metadata["reset_reason"] == "mock_cycle"


def test_lead_below_camera_offset_does_not_trigger_visible_alert() -> None:
    policy = VisionAlertPolicy(
        config={"lead_confirmation_hits": 1, "lead_confirmation_window": 1}
    )
    source = _perception(1, lead_ttc=True)
    close = replace(source, leads=(replace(source.leads[0], x=(1.2,)),))
    assert policy.observe(close) == []


def test_synthetic_fixture_emits_exactly_one_start_and_end_per_alert() -> None:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "front_assistance_policy_v2.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        policy = VisionAlertPolicy()
        transitions = []
        frame = 0
        for _ in range(int(case.get("warmup_frames", 0))):
            frame += 1
            transitions.extend(policy.observe(_perception(frame)))
        signal = str(case["signal"])
        signal_value = case.get("signal_value", True)
        for _ in range(int(case["positive_frames"])):
            frame += 1
            transitions.extend(
                policy.observe(_perception(frame, **{signal: signal_value}))
            )
        for _ in range(int(case["clear_frames"])):
            frame += 1
            transitions.extend(policy.observe(_perception(frame)))
        assert [(item.operation, item.label) for item in transitions] == [
            ("START", case["label"]),
            ("END", case["label"]),
        ]


def test_invalid_and_low_confidence_sequences_never_emit_events() -> None:
    invalid_policy = VisionAlertPolicy()
    low_policy = VisionAlertPolicy()
    assert not any(
        invalid_policy.observe(_perception(frame, valid=False))
        for frame in range(1, 251)
    )
    assert not any(
        low_policy.observe(
            _perception(
                frame,
                left=True,
                right=True,
                lead_ttc=True,
                edge_left=True,
                edge_right=True,
                low_confidence=True,
            )
        )
        for frame in range(1, 251)
    )
