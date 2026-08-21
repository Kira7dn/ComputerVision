"""Pure helpers for building the fixture-backed fire/smoke YOLO dataset."""

from __future__ import annotations

from typing import Any

CLASS_IDS = {"fire": 0, "smoke": 1}


def temporal_split(sample_index: int, block_size: int = 20) -> str:
    """Keep adjacent annotated frames in one deterministic temporal block."""
    if block_size < 1:
        raise ValueError("block_size must be at least 1")
    bucket = (sample_index // block_size) % 10
    if bucket < 7:
        return "train"
    if bucket < 9:
        return "val"
    return "test"


def yolo_labels(
    objects: list[dict[str, Any]], width: int, height: int
) -> list[str]:
    if width < 1 or height < 1:
        raise ValueError("frame dimensions must be positive")
    labels: list[str] = []
    for item in objects:
        classification = str(item.get("class", ""))
        if classification not in CLASS_IDS:
            continue
        left = min(float(width), max(0.0, float(item["x1"])))
        top = min(float(height), max(0.0, float(item["y1"])))
        right = min(float(width), max(0.0, float(item["x2"])))
        bottom = min(float(height), max(0.0, float(item["y2"])))
        if right <= left or bottom <= top:
            continue
        center_x = ((left + right) / 2.0) / width
        center_y = ((top + bottom) / 2.0) / height
        box_width = (right - left) / width
        box_height = (bottom - top) / height
        labels.append(
            f"{CLASS_IDS[classification]} {center_x:.8f} {center_y:.8f} "
            f"{box_width:.8f} {box_height:.8f}"
        )
    return labels
