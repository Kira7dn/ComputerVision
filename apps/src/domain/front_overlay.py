"""Pure projection contract for high-value front-camera overlay geometry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from domain.front_assistance import FrontPerception

Point = tuple[int, int]


@dataclass(frozen=True)
class ProjectedLane:
    lane_index: int
    confidence: float
    points: tuple[Point, ...]


@dataclass(frozen=True)
class FrontOverlayGeometry:
    lanes: tuple[ProjectedLane, ...]
    path_center: tuple[Point, ...]
    path_left: tuple[Point, ...]
    path_right: tuple[Point, ...]
    path_source: str

    def summary(self) -> dict[str, Any]:
        lane_segments = sum(max(0, len(lane.points) - 1) for lane in self.lanes)
        path_segments = (
            max(0, len(self.path_center) - 1)
            + max(0, len(self.path_left) - 1)
            + max(0, len(self.path_right) - 1)
            + min(len(self.path_left), len(self.path_right))
        )
        return {
            "visible_lane_count": len(self.lanes),
            "lane_segment_count": lane_segments,
            "path_point_count": len(self.path_center),
            "path_segment_count": path_segments,
            "path_source": self.path_source,
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
    lane_min_probability: float = 0.5,
    path_half_width_m: float = 0.9,
) -> FrontOverlayGeometry:
    """Project model-space lanes and a predicted path corridor into image pixels."""
    intrinsic = calibration.get("intrinsics", [])
    if (
        len(intrinsic) != 3
        or width <= 0
        or height <= 0
        or lane_min_probability < 0.0
        or path_half_width_m <= 0.0
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
    for lane_index in (1, 2):
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
    return FrontOverlayGeometry(
        tuple(lanes),
        path_center,
        path_left,
        path_right,
        "model_position",
    )
