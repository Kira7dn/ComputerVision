"""A/B characterization that separates orchestration from model quality."""

from __future__ import annotations

from typing import Any


def characterize_orchestration(source: str) -> dict[str, bool]:
    hard_suppression_patterns = (
        "if self._person_box_overlaps_fresh_fire_smoke(box):\n                    continue",
        "if not environment_excluded:\n                track[\"confirmation\"].observe",
    )
    return {
        "independent_function_executors": (
            "LatestSampleExecutor" in source
            and '"smoking_behavior"' in source
            and '"fire_smoke"' in source
        ),
        "bounded_latest_only_queues": "queue.Queue[AnalysisSample | None]" in source
        or "LatestSampleExecutor" in source,
        "frame_identity_uses_pts": (
            "FrameKey(" in source and 'getattr(frame_meta, "buf_pts"' in source
        ),
        "stale_event_results_rejected": (
            "FrameResultGate" in source
            and "self._analysis_gate.evaluate" in source
            and 'decision.reason == "stale"' in source
        ),
        "person_roi_is_same_frame": (
            "AnalysisSample(" in source
            and "persons=tuple(persons)" in source
            and "persons = list(self._latest_person_rois)" not in source
        ),
        "fire_runs_without_person": (
            "fire_smoke_engine.process(sample.frame)" in source
            or "fire_smoke_engine.process(frame)" in source
        ),
        "fire_overlap_suppresses_person": any(
            pattern in source for pattern in hard_suppression_patterns
        ),
    }


def compare_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    repeatability_tolerance: float = 0.01,
) -> dict[str, Any]:
    baseline_metrics = baseline["metrics"]
    candidate_metrics = candidate["metrics"]
    macro_f1_delta = float(candidate_metrics["macro_f1"]) - float(
        baseline_metrics["macro_f1"]
    )
    class_deltas = {
        label: {
            metric: float(candidate_metrics["classes"][label][metric])
            - float(baseline_metrics["classes"][label][metric])
            for metric in ("precision", "recall", "f1")
        }
        for label in baseline_metrics["classes"]
    }
    return {
        "macro_f1_delta": macro_f1_delta,
        "class_deltas": class_deltas,
        "within_repeatability_tolerance": abs(macro_f1_delta)
        <= repeatability_tolerance,
        "raw_model_quality_improved": macro_f1_delta > repeatability_tolerance,
        "smoke_recall_improved": class_deltas["smoke"]["recall"] > 0.0,
        "candidate_no_regression_accepted": candidate.get("accepted") is True,
    }


def architecture_improvements(
    baseline: dict[str, bool], candidate: dict[str, bool]
) -> dict[str, bool]:
    return {
        "function_execution_isolated": (
            not baseline["independent_function_executors"]
            and candidate["independent_function_executors"]
        ),
        "frame_alignment_added": (
            not baseline["frame_identity_uses_pts"]
            and candidate["frame_identity_uses_pts"]
            and candidate["person_roi_is_same_frame"]
        ),
        "stale_event_gate_added": (
            not baseline["stale_event_results_rejected"]
            and candidate["stale_event_results_rejected"]
        ),
        "person_overlap_suppression_removed": (
            baseline["fire_overlap_suppresses_person"]
            and not candidate["fire_overlap_suppresses_person"]
        ),
        "fire_without_person_preserved": (
            baseline["fire_runs_without_person"]
            and candidate["fire_runs_without_person"]
        ),
    }
