"""Deterministic frame-level scoring for Safety fixture replays."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def score_presence(
    rows: Iterable[tuple[set[str], set[str]]],
    labels: tuple[str, ...] = ("fire", "smoke"),
) -> dict[str, Any]:
    materialized = list(rows)
    classes: dict[str, dict[str, float | int]] = {}
    for label in labels:
        true_positive = sum(
            int(label in expected and label in predicted)
            for expected, predicted in materialized
        )
        false_positive = sum(
            int(label not in expected and label in predicted)
            for expected, predicted in materialized
        )
        false_negative = sum(
            int(label in expected and label not in predicted)
            for expected, predicted in materialized
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 1.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 1.0
        )
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        classes[label] = {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return {
        "sample_count": len(materialized),
        "classes": classes,
        "macro_f1": sum(float(classes[label]["f1"]) for label in labels)
        / len(labels),
    }


def compare_with_baseline(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    *,
    per_metric_tolerance: float = 0.02,
    onset_latency_tolerance_seconds: float = 0.20,
) -> dict[str, bool]:
    gates: dict[str, bool] = {
        "macro_f1_no_regression": float(candidate["macro_f1"])
        >= float(baseline["macro_f1"]),
        "false_negatives_no_increase": sum(
            int(item["false_negative"])
            for item in candidate["classes"].values()
        )
        <= sum(
            int(item["false_negative"])
            for item in baseline["classes"].values()
        ),
    }
    for label, candidate_class in candidate["classes"].items():
        baseline_class = baseline["classes"][label]
        gates[f"{label}_precision"] = float(candidate_class["precision"]) >= (
            float(baseline_class["precision"]) - per_metric_tolerance
        )
        gates[f"{label}_recall"] = float(candidate_class["recall"]) >= (
            float(baseline_class["recall"]) - per_metric_tolerance
        )
    if (
        candidate.get("event_onset_p95_seconds") is not None
        and baseline.get("event_onset_p95_seconds") is not None
    ):
        gates["event_onset_p95"] = float(candidate["event_onset_p95_seconds"]) <= (
            float(baseline["event_onset_p95_seconds"])
            + onset_latency_tolerance_seconds
        )
    return gates
