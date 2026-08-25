from ls_vision.application.safety_replay import compare_with_baseline, score_presence


def test_presence_metrics_are_balanced_per_class() -> None:
    metrics = score_presence(
        [
            ({"fire"}, {"fire"}),
            ({"fire"}, set()),
            (set(), {"smoke"}),
            ({"smoke"}, {"smoke"}),
        ]
    )

    assert metrics["classes"]["fire"]["precision"] == 1.0
    assert metrics["classes"]["fire"]["recall"] == 0.5
    assert metrics["classes"]["smoke"]["precision"] == 0.5
    assert metrics["classes"]["smoke"]["recall"] == 1.0
    assert metrics["macro_f1"] == 2 / 3


def test_baseline_gate_rejects_f1_and_false_negative_regression() -> None:
    baseline = score_presence([({"fire", "smoke"}, {"fire", "smoke"})])
    candidate = score_presence([({"fire", "smoke"}, {"fire"})])

    gates = compare_with_baseline(candidate, baseline)

    assert not gates["macro_f1_no_regression"]
    assert not gates["false_negatives_no_increase"]
    assert not gates["smoke_recall"]
