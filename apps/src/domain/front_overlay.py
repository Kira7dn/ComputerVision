"""Pure projection contract for high-value front-camera overlay geometry."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, TypeVar

import numpy as np

from domain.front_assistance import FrontPerception

Point = tuple[int, int]
T = TypeVar("T")


@dataclass(frozen=True)
class ProjectedLane:
    lane_index: int
    confidence: float
    points: tuple[Point, ...]


@dataclass(frozen=True)
class ProjectedRoadEdge:
    edge_index: int
    uncertainty: float
    opacity: float
    points: tuple[Point, ...]


@dataclass(frozen=True)
class ProjectedLead:
    lead_index: int
    probability: float
    distance_m: float
    relative_velocity_mps: float
    point: Point
    glow: tuple[Point, Point, Point]
    chevron: tuple[Point, Point, Point]
    fill_alpha: float


@dataclass(frozen=True)
class ProjectedHorizon:
    seconds: float
    point: Point


@dataclass(frozen=True)
class FrontOverlayText:
    text: str
    x: int
    y: int
    font_size: int = 14
    color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)


@dataclass(frozen=True)
class FrontOverlayGeometry:
    lanes: tuple[ProjectedLane, ...]
    path_center: tuple[Point, ...]
    path_left: tuple[Point, ...]
    path_right: tuple[Point, ...]
    path_source: str
    road_edges: tuple[ProjectedRoadEdge, ...] = ()
    leads: tuple[ProjectedLead, ...] = ()
    horizons: tuple[ProjectedHorizon, ...] = ()

    def summary(self) -> dict[str, Any]:
        lane_segments = sum(max(0, len(lane.points) - 1) for lane in self.lanes)
        path_segments = (
            max(0, len(self.path_center) - 1)
            + max(0, len(self.path_left) - 1)
            + max(0, len(self.path_right) - 1)
            + min(len(self.path_left), len(self.path_right))
        )
        road_edge_segments = sum(
            max(0, len(edge.points) - 1) for edge in self.road_edges
        )
        # Each openpilot lead marker is made from an outer glow triangle and an
        # inner chevron triangle. Runtime rasterization may use more scanlines,
        # but the geometry contract remains six triangle edges per marker.
        lead_segments = len(self.leads) * 6
        return {
            "visible_lane_count": len(self.lanes),
            "lane_segment_count": lane_segments,
            "path_point_count": len(self.path_center),
            "path_segment_count": path_segments,
            "path_source": self.path_source,
            "visible_road_edge_count": len(self.road_edges),
            "road_edge_segment_count": road_edge_segments,
            "visible_lead_count": len(self.leads),
            "lead_segment_count": lead_segments,
            "lead_chevron_count": len(self.leads),
            "lead_style": "openpilot_chevron",
            "horizon_marker_count": len(self.horizons),
            "lane_confidences": {
                str(lane.lane_index): round(lane.confidence, 5)
                for lane in self.lanes
            },
        }


def project_front_overlay(
    perception: FrontPerception,
    calibration: dict[str, Any],
    *,
    width: int,
    height: int,
    lane_min_probability: float = 0.0,
    path_half_width_m: float = 0.9,
    lead_min_probability: float = 0.5,
    road_edge_max_std_m: float = 0.6,
) -> FrontOverlayGeometry:
    """Project model-space lanes and a predicted path corridor into image pixels."""
    intrinsic = calibration.get("intrinsics", [])
    if (
        len(intrinsic) != 3
        or width <= 0
        or height <= 0
        or lane_min_probability < 0.0
        or path_half_width_m <= 0.0
        or not 0.0 <= lead_min_probability <= 1.0
        or road_edge_max_std_m <= 0.0
    ):
        return FrontOverlayGeometry((), (), (), (), "unavailable")
    camera_height = float(calibration.get("camera_height_m", 1.51))
    rpy = tuple(float(value) for value in calibration.get("rpy_calib", [0.0, 0.0, 0.0]))
    if len(rpy) != 3 or not np.isfinite(camera_height) or camera_height <= 0.0:
        return FrontOverlayGeometry((), (), (), (), "unavailable")

    roll, pitch, yaw = rpy
    sr, cr = np.sin(roll), np.cos(roll)
    sp, cp = np.sin(pitch), np.cos(pitch)
    sy, cy = np.sin(yaw), np.cos(yaw)
    device_from_calib = np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=np.float64,
    )
    view_from_device = np.array(
        [[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]],
        dtype=np.float64,
    )
    projection = (
        np.asarray(intrinsic, dtype=np.float64)
        @ view_from_device
        @ device_from_calib
    )

    def project(x: float, y: float, z: float) -> Point | None:
        if x <= 0.1:
            return None
        transformed = projection @ np.asarray((x, y, z), dtype=np.float64)
        if not np.isfinite(transformed).all() or transformed[2] <= 1e-6:
            return None
        px = int(round(float(transformed[0] / transformed[2])))
        py = int(round(float(transformed[1] / transformed[2])))
        if px < 0 or px >= width or py < 0 or py >= height:
            return None
        return px, py

    lanes: list[ProjectedLane] = []
    for lane_index in range(4):
        if (
            lane_index >= len(perception.lane_lines)
            or lane_index >= len(perception.lane_probabilities)
        ):
            continue
        confidence = perception.lane_probabilities[lane_index]
        if confidence < lane_min_probability:
            continue
        points = tuple(
            point
            for x, y, z in perception.lane_lines[lane_index][2:29:4]
            if (point := project(x, y, z)) is not None
        )
        if len(points) >= 2:
            lanes.append(ProjectedLane(lane_index, confidence, points))

    # openpilot keeps the model's longitudinal position. A short camera-only
    # plan is allowed to remain short; stretching it would create non-metric
    # geometry that looks authoritative but is not model output.
    sampled_path = perception.path[2:29:4]
    path_center = tuple(
        point
        for x, y, z in sampled_path
        if (point := project(x, y, z + camera_height)) is not None
    )
    path_left = tuple(
        point
        for x, y, z in sampled_path
        if (point := project(x, y - path_half_width_m, z + camera_height)) is not None
    )
    path_right = tuple(
        point
        for x, y, z in sampled_path
        if (point := project(x, y + path_half_width_m, z + camera_height)) is not None
    )
    road_edges: list[ProjectedRoadEdge] = []
    for edge_index, edge in enumerate(perception.road_edges[:2]):
        stds = (
            perception.road_edge_stds[edge_index]
            if edge_index < len(perception.road_edge_stds)
            else ()
        )
        points = tuple(
            point
            for x, y, z in edge[2:29:4]
            if (point := project(x, y, z)) is not None
        )
        std_values = [
            stds[index][0]
            for index in range(2, min(29, len(stds)), 4)
            if math.isfinite(stds[index][0])
        ]
        uncertainty = max(std_values, default=math.inf)
        opacity = (
            max(0.15, min(1.0, 1.0 - uncertainty / road_edge_max_std_m))
            if math.isfinite(uncertainty)
            else 0.15
        )
        if len(points) >= 2:
            road_edges.append(
                ProjectedRoadEdge(edge_index, uncertainty, opacity, points)
            )

    projected_leads: list[ProjectedLead] = []
    model_v_ego = (
        perception.plan_velocity[0][0] if perception.plan_velocity else 0.0
    )
    # openpilot's on-road UI renders radarState leadOne/leadTwo, which are
    # derived from the first two model leads in vision-only mode. It does not
    # render a bounding box or the raw multi-point lead trajectories.
    for lead_index, lead in enumerate(perception.leads[:2]):
        if (
            lead.probability < lead_min_probability
            or not lead.x
            or not lead.y
            or not lead.velocity
        ):
            continue
        # Match radard's vision-only conversion before the UI projection.
        distance_m = lead.x[0] - 1.52
        relative_velocity_mps = lead.velocity[0] - model_v_ego
        path_z = min(
            perception.path,
            key=lambda item: abs(item[0] - distance_m),
            default=(0.0, 0.0, 0.0),
        )[2]
        point = project(distance_m, lead.y[0], path_z + camera_height)
        if point is None:
            continue

        # Ported from openpilot selfdrive/ui/onroad/model_renderer.py.
        size = float(
            np.clip((25.0 * 30.0) / (distance_m / 3.0 + 30.0), 15.0, 30.0)
            * 2.35
        )
        marker_x = float(np.clip(point[0], 0.0, width - size / 2.0))
        marker_y = min(float(point[1]), height - size * 0.6)
        glow_x = size / 5.0
        glow_y = size / 10.0

        def screen_point(x: float, y: float) -> Point:
            return (
                int(round(np.clip(x, 0.0, width - 1.0))),
                int(round(np.clip(y, 0.0, height - 1.0))),
            )

        glow = (
            screen_point(marker_x + size * 1.35 + glow_x, marker_y + size + glow_y),
            screen_point(marker_x, marker_y - glow_y),
            screen_point(marker_x - size * 1.35 - glow_x, marker_y + size + glow_y),
        )
        chevron = (
            screen_point(marker_x + size * 1.25, marker_y + size),
            screen_point(marker_x, marker_y),
            screen_point(marker_x - size * 1.25, marker_y + size),
        )
        fill_alpha = 0.0
        if distance_m < 40.0:
            fill_alpha = 1.0 - distance_m / 40.0
            if relative_velocity_mps < 0.0:
                fill_alpha += -relative_velocity_mps / 10.0
            fill_alpha = min(fill_alpha, 1.0)
        projected_leads.append(
            ProjectedLead(
                lead_index,
                lead.probability,
                distance_m,
                relative_velocity_mps,
                point,
                glow,
                chevron,
                fill_alpha,
            )
        )

    horizons: list[ProjectedHorizon] = []
    if perception.plan_times and perception.path:
        candidate_count = min(len(perception.plan_times), len(perception.path))
        for seconds in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0):
            for index in sorted(
                range(candidate_count),
                key=lambda item: abs(perception.plan_times[item] - seconds),
            ):
                x, y, z = perception.path[index]
                if (point := project(x, y, z + camera_height)) is not None:
                    horizons.append(ProjectedHorizon(seconds, point))
                    break
    return FrontOverlayGeometry(
        tuple(lanes),
        path_center,
        path_left,
        path_right,
        "model_position",
        tuple(road_edges),
        tuple(projected_leads),
        tuple(horizons),
    )


def build_front_hud(
    perception: FrontPerception,
    *,
    banner_label: str | None,
    fps: float,
    recording: bool,
    width: int,
    height: int,
    now: dt.datetime | None = None,
    geometry_diagnostics: dict[str, Any] | None = None,
) -> tuple[FrontOverlayText, ...]:
    """Build only the active advisory banner; diagnostics stay in metadata."""
    if not perception.valid or perception.readiness.value != "ready":
        reason = ",".join(perception.blocking_reasons) or "không đủ dữ liệu"
        return (
            FrontOverlayText(
                f"HỖ TRỢ LÁI CHƯA SẴN SÀNG | {reason}",
                24,
                24,
                24,
                (1.0, 0.75, 0.1, 1.0),
            ),
        )

    alert_names = {
        "vision_fcw": "CẢNH BÁO VA CHẠM",
        "vision_lead_ttc": "QUÁ GẦN XE PHÍA TRƯỚC",
        "vision_road_edge_left": "SÁT MÉP ĐƯỜNG TRÁI",
        "vision_road_edge_right": "SÁT MÉP ĐƯỜNG PHẢI",
        "vision_ldw_left": "LỆCH LÀN TRÁI",
        "vision_ldw_right": "LỆCH LÀN PHẢI",
        "vision_geometry_drift": "KIỂM TRA VỊ TRÍ CAMERA",
    }
    if banner_label is None:
        return ()
    return (
        FrontOverlayText(
            alert_names.get(banner_label, "CẢNH BÁO CAMERA TRƯỚC"),
            24,
            24,
            24,
            (1.0, 0.2, 0.1, 1.0),
        ),
    )


def chunk_osd_items(
    items: tuple[T, ...] | list[T], size: int = 16
) -> tuple[tuple[T, ...], ...]:
    if size < 1 or size > 16:
        raise ValueError("NvDsDisplayMeta chunk size must be in [1, 16]")
    return tuple(tuple(items[offset : offset + size]) for offset in range(0, len(items), size))
