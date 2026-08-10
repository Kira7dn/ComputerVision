"""Pure passage scoring and correlation helpers used by tests and validators."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any


def normalize_plate(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def bbox_iou(left: list[float], right: list[float]) -> float:
    ax1, ay1, ax2, ay2 = left; bx1, by1, bx2, by2 = right
    ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1) + max(0.0, bx2 - bx1) * max(0.0, by2 - by1) - intersection
    return intersection / union if union else 0.0


def match_by_time_and_bbox(predictions: list[dict[str, Any]], passages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Associate each result with one physical passage, never timestamp alone."""
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prediction in predictions:
        timestamp = float(prediction.get("frame_time", prediction.get("timestamp", -1)))
        box = prediction.get("bbox") or prediction.get("object_box")
        candidates = [p for p in passages if float(p["start_s"]) <= timestamp <= float(p["end_s"])]
        if box is not None:
            candidates = [p for p in candidates if bbox_iou([float(x) for x in box], [float(x) for x in p["bbox"]]) >= 0.05]
        if len(candidates) == 1:
            result[str(candidates[0]["id"])].append(prediction)
    return dict(result)


def score_face_passages(predictions: list[dict[str, Any]], passages: list[dict[str, Any]]) -> dict[str, Any]:
    matched = match_by_time_and_bbox(predictions, passages)
    rows = []
    for passage in passages:
        values = matched.get(str(passage["id"]), [])
        labels = [str(v.get("identity") or "unknown") for v in values]
        expected = str(passage["expected_identity"])
        positives = [label for label in labels if label != "unknown"]
        correct = bool(labels) and (expected == "unknown" and not positives or expected != "unknown" and expected in positives)
        false_positive = expected == "unknown" and bool(positives)
        rows.append({"passage_id": passage["id"], "expected": expected, "predictions": labels, "detected": bool(values), "correct": correct, "false_positive": false_positive})
    detected = sum(row["detected"] for row in rows)
    correct = sum(row["correct"] for row in rows)
    return {"passages": rows, "detection_recall": detected / len(rows) if rows else 0.0, "precision": correct / detected if detected else 0.0, "recall": correct / len(rows) if rows else 0.0, "false_positives": sum(row["false_positive"] for row in rows)}


def percentile(values: list[float], number: float) -> float | None:
    if not values: return None
    ordered = sorted(values); index = (len(ordered) - 1) * number / 100
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (index - low)
