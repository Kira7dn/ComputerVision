from dataclasses import replace

from ls_vision.domain.front_assistance import FrontPerception, FrontReadiness
from ls_vision.domain.front_overlay import project_front_overlay


def perception() -> FrontPerception:
    distances = tuple(float(index * 4) for index in range(33))
    return FrontPerception(
        source_epoch="front-1",
        frame_number=10,
        source_timestamp=100.0,
        valid=True,
        readiness=FrontReadiness.READY,
        blocking_reasons=(),
        lane_lines=(
            tuple((x, -3.0, 1.2) for x in distances),
            tuple((x, -1.5, 1.2) for x in distances),
            tuple((x, 1.5, 1.2) for x in distances),
            tuple((x, 3.0, 1.2) for x in distances),
        ),
        lane_probabilities=(0.01, 0.80, 0.60, 0.01),
        road_edges=(),
        path=tuple((x, 0.0, 0.0) for x in distances),
        leads=(),
        desire_prediction=(),
        hard_brake_predicted=False,
        hard_brake_3_probs=(),
        hard_brake_5_probs=(),
        provider="test",
        inference_ms=10.0,
        model_hash="model",
        calibration_hash="calibration",
    )


def calibration() -> dict[str, object]:
    return {
        "intrinsics": [
            [759.85, 0.0, 489.76],
            [0.0, 759.85, 294.90],
            [0.0, 0.0, 1.0],
        ],
        "rpy_calib": [0.0, 0.0, 0.0],
        "camera_height_m": 1.51,
    }


def test_confident_lane_xyz_and_metric_path_are_projected() -> None:
    geometry = project_front_overlay(
        perception(), calibration(), width=960, height=540
    )
    summary = geometry.summary()

    assert summary["visible_lane_count"] == 2
    assert summary["lane_segment_count"] > 0
    assert summary["path_point_count"] >= 2
    assert summary["path_segment_count"] > summary["path_point_count"]
    assert summary["path_source"] == "model_position"
    assert summary["lane_confidences"] == {"1": 0.8, "2": 0.6}
    assert geometry.lanes[0].points[0] == (347, 409)


def test_lane_threshold_does_not_remove_predicted_path() -> None:
    geometry = project_front_overlay(
        perception(),
        calibration(),
        width=960,
        height=540,
        lane_min_probability=0.9,
    )

    assert geometry.lanes == ()
    assert len(geometry.path_center) >= 2


def test_short_vision_only_plan_is_not_stretched_into_fake_depth() -> None:
    source = perception()
    short_path = tuple(
        (index / 10.0, index / 100.0, 0.0) for index in range(33)
    )

    geometry = project_front_overlay(
        replace(source, path=short_path), calibration(), width=960, height=540
    )

    assert geometry.path_source == "model_position"
    assert geometry.path_center == ()
    assert geometry.path_left == ()
    assert geometry.path_right == ()


def test_low_confidence_lanes_fail_closed_by_default() -> None:
    source = replace(
        perception(), lane_probabilities=(0.01, 0.49, 0.20, 0.01)
    )

    geometry = project_front_overlay(
        source, calibration(), width=960, height=540
    )

    assert geometry.lanes == ()
    assert geometry.summary()["lane_confidences"] == {}


def test_invalid_calibration_fails_closed_without_geometry() -> None:
    geometry = project_front_overlay(
        perception(), {}, width=960, height=540
    )

    assert geometry.summary() == {
        "visible_lane_count": 0,
        "lane_segment_count": 0,
        "path_point_count": 0,
        "path_segment_count": 0,
        "path_source": "unavailable",
        "lane_confidences": {},
    }
