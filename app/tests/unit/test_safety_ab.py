from application.safety_ab import (
    architecture_improvements,
    characterize_orchestration,
    compare_reports,
)


def test_characterization_distinguishes_shared_legacy_loop_from_frame_gate() -> None:
    legacy = """
fire_smoke_engine.process(frame)
persons = list(self._latest_person_rois)
if self._person_box_overlaps_fresh_fire_smoke(box):
                    continue
"""
    candidate = """
LatestSampleExecutor
queue.Queue[AnalysisSample | None]
\"smoking_behavior\" \"fire_smoke\"
FrameKey(
getattr(frame_meta, \"buf_pts\"
FrameResultGate
self._analysis_gate.evaluate
decision.reason == \"stale\"
AnalysisSample(
persons=tuple(persons)
fire_smoke_engine.process(sample.frame)
"""

    before = characterize_orchestration(legacy)
    after = characterize_orchestration(candidate)
    improvements = architecture_improvements(before, after)

    assert all(improvements.values())


def test_raw_model_delta_inside_repeatability_is_not_an_improvement() -> None:
    baseline = {
        "metrics": {
            "macro_f1": 0.1778,
            "classes": {
                "fire": {"precision": 0.22, "recall": 0.86, "f1": 0.35},
                "smoke": {"precision": 1.0, "recall": 0.0, "f1": 0.0},
            },
        }
    }
    candidate = {
        "accepted": True,
        "metrics": {
            "macro_f1": 0.1788,
            "classes": {
                "fire": {"precision": 0.225, "recall": 0.86, "f1": 0.3576},
                "smoke": {"precision": 1.0, "recall": 0.0, "f1": 0.0},
            },
        },
    }

    comparison = compare_reports(baseline, candidate)

    assert comparison["within_repeatability_tolerance"]
    assert not comparison["raw_model_quality_improved"]
    assert not comparison["smoke_recall_improved"]
