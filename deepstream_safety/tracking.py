"""Pure geometry helpers for application-level person tracking."""

from __future__ import annotations

import numpy as np


def iou(left: np.ndarray, right: np.ndarray) -> float:
    x1 = max(float(left[0]), float(right[0]))
    y1 = max(float(left[1]), float(right[1]))
    x2 = min(float(left[2]), float(right[2]))
    y2 = min(float(left[3]), float(right[3]))
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_left = max(0.0, float(left[2] - left[0])) * max(
        0.0, float(left[3] - left[1])
    )
    area_right = max(0.0, float(right[2] - right[0])) * max(
        0.0, float(right[3] - right[1])
    )
    union = area_left + area_right - intersection
    return intersection / union if union > 0 else 0.0


def frigate_track_distance(detection: np.ndarray, estimate: np.ndarray) -> float:
    """Normalize position and size changes like Frigate's Norfair tracker."""
    estimate_dim = np.diff(estimate.reshape(2, 2), axis=0).flatten()
    detection_dim = np.diff(detection.reshape(2, 2), axis=0).flatten()
    if (
        not np.all(np.isfinite(estimate_dim))
        or not np.all(np.isfinite(detection_dim))
        or np.any(estimate_dim <= 0)
        or np.any(detection_dim <= 0)
    ):
        return float("inf")

    detection_position = np.array(
        [(detection[0] + detection[2]) / 2.0, detection[3]], dtype=np.float32
    )
    estimate_position = np.array(
        [(estimate[0] + estimate[2]) / 2.0, estimate[3]], dtype=np.float32
    )
    position_delta = detection_position - estimate_position
    position_delta[0] /= estimate_dim[0]
    position_delta[1] /= estimate_dim[1]
    widths = np.sort([estimate_dim[0], detection_dim[0]])
    heights = np.sort([estimate_dim[1], detection_dim[1]])
    change = np.append(
        position_delta,
        np.array([widths[1] / widths[0] - 1.0, heights[1] / heights[0] - 1.0]),
    )
    return float(np.linalg.norm(change))


def opposite_frame_edge_transition(
    previous: np.ndarray, current: np.ndarray, width: float, height: float
) -> bool:
    """Detect a true jump between opposite edges, excluding edge-spanning boxes."""
    x_margin = width * 0.025
    y_margin = height * 0.025
    previous_left = previous[0] <= x_margin
    previous_top = previous[1] <= y_margin
    previous_right = previous[2] >= width - x_margin
    previous_bottom = previous[3] >= height - y_margin
    current_left = current[0] <= x_margin
    current_top = current[1] <= y_margin
    current_right = current[2] >= width - x_margin
    current_bottom = current[3] >= height - y_margin

    previous_left_only = previous_left and not previous_right
    previous_right_only = previous_right and not previous_left
    previous_top_only = previous_top and not previous_bottom
    previous_bottom_only = previous_bottom and not previous_top
    current_left_only = current_left and not current_right
    current_right_only = current_right and not current_left
    current_top_only = current_top and not current_bottom
    current_bottom_only = current_bottom and not current_top
    return (
        (previous_left_only and current_right_only)
        or (previous_right_only and current_left_only)
        or (previous_top_only and current_bottom_only)
        or (previous_bottom_only and current_top_only)
    )
