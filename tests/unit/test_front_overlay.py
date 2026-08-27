from dataclasses import replace

import pytest

from domain.front_assistance import FrontLead, FrontPerception, FrontReadiness
from domain.front_overlay import build_front_hud, chunk_osd_items, project_front_overlay


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
        road_edges=(
            tuple((x, -3.8, 1.2) for x in distances),
            tuple((x, 3.8, 1.2) for x in distances),
        ),
        path=tuple((x, 0.0, 0.0) for x in distances),
        leads=tuple(
            FrontLead(
                probability=0.9 - index * 0.1,
                x=tuple(12.0 + index * 4.0 + offset for offset in range(6)),
                y=tuple(float(index - 1) for _ in range(6)),
                velocity=tuple(-2.0 for _ in range(6)),
                acceleration=tuple(0.0 for _ in range(6)),
            )
            for index in range(3)
        ),
        desire_prediction=(),
        hard_brake_predicted=False,
        hard_brake_3_probs=(),
        hard_brake_5_probs=(),
        provider="test",
        inference_ms=10.0,
        model_hash="model",
        calibration_hash="calibration",
        road_edge_stds=tuple(
            tuple((0.2 + index * 0.1, 0.1) for _ in distances)
            for index in range(2)
        ),
        plan_times=tuple(10.0 * ((index / 32.0) ** 2) for index in range(33)),
        pose=(1.0, 2.0, 3.0, 0.01, 0.02, 0.03),
        road_transform=(0.1, 0.2, 0.3, 0.0, 0.0, 0.0),
        wide_from_device_euler=(0.01, 0.02, 0.03),
        confidence="green",
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

    assert summary["visible_lane_count"] == 4
    assert summary["lane_segment_count"] > 0
    assert summary["path_point_count"] >= 2
    assert summary["path_segment_count"] > summary["path_point_count"]
    assert summary["path_source"] == "model_position"
    assert summary["visible_road_edge_count"] == 2
    assert summary["visible_lead_count"] == 2
    assert summary["lead_chevron_count"] == 2
    assert summary["lead_segment_count"] == 12
    assert summary["lead_style"] == "openpilot_chevron"
    assert summary["horizon_marker_count"] == 6
    assert summary["lane_confidences"] == {
        "0": 0.01,
        "1": 0.8,
        "2": 0.6,
        "3": 0.01,
    }
    assert geometry.lanes[1].points[0] == (347, 409)
    assert tuple(lead.lead_index for lead in geometry.leads) == (0, 1)
    assert all(len(lead.glow) == len(lead.chevron) == 3 for lead in geometry.leads)
    assert all(0.0 <= lead.fill_alpha <= 1.0 for lead in geometry.leads)


def test_leads_use_openpilot_chevrons_without_raw_trajectory() -> None:
    geometry = project_front_overlay(
        perception(), calibration(), width=960, height=540
    )

    lead = geometry.leads[0]
    assert lead.distance_m == pytest.approx(10.48)
    assert lead.relative_velocity_mps == pytest.approx(-2.0)
    assert lead.point == lead.chevron[1]
    assert not hasattr(lead, "points")
    assert lead.fill_alpha == pytest.approx(0.938)


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


def test_low_confidence_lanes_remain_visible_for_opacity_rendering() -> None:
    source = replace(
        perception(), lane_probabilities=(0.01, 0.49, 0.20, 0.01)
    )

    geometry = project_front_overlay(
        source, calibration(), width=960, height=540
    )

    assert len(geometry.lanes) == 4
    assert geometry.summary()["lane_confidences"] == {
        "0": 0.01,
        "1": 0.49,
        "2": 0.2,
        "3": 0.01,
    }


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
        "visible_road_edge_count": 0,
        "road_edge_segment_count": 0,
        "visible_lead_count": 0,
        "lead_segment_count": 0,
        "lead_chevron_count": 0,
        "lead_style": "openpilot_chevron",
        "horizon_marker_count": 0,
        "lane_confidences": {},
    }


def test_hud_only_renders_priority_banner() -> None:
    labels = build_front_hud(
        perception(),
        banner_label="vision_fcw",
        fps=20.0,
        recording=True,
        width=960,
        height=540,
    )

    assert tuple(label.text for label in labels) == ("CẢNH BÁO VA CHẠM",)


def test_hud_renders_no_text_without_an_alert() -> None:
    labels = build_front_hud(
        perception(),
        banner_label=None,
        fps=20.0,
        recording=True,
        width=960,
        height=540,
    )

    assert labels == ()


@pytest.mark.parametrize(
    ("classification", "expected_text"),
    (
        ("vision_fcw", "CẢNH BÁO VA CHẠM"),
        ("vision_lead_ttc", "QUÁ GẦN XE PHÍA TRƯỚC"),
        ("vision_road_edge_left", "SÁT MÉP ĐƯỜNG TRÁI"),
        ("vision_road_edge_right", "SÁT MÉP ĐƯỜNG PHẢI"),
        ("vision_ldw_left", "LỆCH LÀN TRÁI"),
        ("vision_ldw_right", "LỆCH LÀN PHẢI"),
        ("vision_geometry_drift", "KIỂM TRA VỊ TRÍ CAMERA"),
    ),
)
def test_hud_uses_short_vietnamese_alert_labels(
    classification: str, expected_text: str
) -> None:
    labels = build_front_hud(
        perception(),
        banner_label=classification,
        fps=20.0,
        recording=True,
        width=960,
        height=540,
    )

    assert labels[0].text == expected_text


def test_osd_chunks_never_exceed_nvds_capacity() -> None:
    chunks = chunk_osd_items(list(range(35)))
    assert tuple(len(chunk) for chunk in chunks) == (16, 16, 3)
    with pytest.raises(ValueError, match="chunk size"):
        chunk_osd_items([1], 17)
