import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.prepare_baseline_fixture import load_manifest
from tools.validate_face_replay import (
    face_event_result,
    score_ground_truth,
)
from tools.validate_lpr_acceptance import match_passage

MANIFEST = ROOT / "tools/fixtures/platform_baseline_ground_truth.yaml"


def write_manifest(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def test_manifest_is_valid_and_bounded() -> None:
    manifest = load_manifest(MANIFEST, ROOT)
    assert manifest["test_case_limit_seconds"] < 120
    assert len(manifest["face"]["cases"]) == 2


def test_manifest_rejects_duplicate_passage(tmp_path: Path) -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    manifest["lpr"]["passages"][1]["id"] = manifest["lpr"]["passages"][0]["id"]
    with pytest.raises(ValueError, match="unique"):
        load_manifest(write_manifest(tmp_path, manifest), ROOT)


def test_manifest_rejects_readable_without_plate(tmp_path: Path) -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    manifest["lpr"]["passages"][0]["readable"] = True
    with pytest.raises(ValueError, match="requires expected_plate"):
        load_manifest(write_manifest(tmp_path, manifest), ROOT)


def test_passage_matching_survives_replay_loop() -> None:
    passages = [{"id": "p1", "start_s": 1.0, "end_s": 2.0}]
    assert match_passage(101.5, 100.0, 15.0, passages)["id"] == "p1"
    assert match_passage(116.5, 100.0, 15.0, passages)["id"] == "p1"


@pytest.mark.parametrize(
    ("predictions", "expected", "score"),
    [
        (["unknown", "P1", "unknown"], "P1", (1.0, 1.0)),
        (["unknown", "unknown"], "unknown", (1.0, 1.0)),
        (["P1"], "unknown", (0.0, 0.0)),
    ],
)
def test_known_unknown_face_scoring(
    predictions: list[str], expected: str, score: tuple[float, float]
) -> None:
    assert score_ground_truth(predictions, expected) == score


def test_evidence_mismatch_is_visible() -> None:
    event = {
        "id": "event-1",
        "start_time": 10.0,
        "sub_label": "P1",
        "data": {
            "box": [0.1, 0.1, 0.3, 0.3],
            "face_box": [0.8, 0.8, 0.1, 0.1],
            "face_snapshot_frame_time": 10.2,
            "face_snapshot_sub_label": "P1",
        },
    }
    assert face_event_result(deepcopy(event))["candidate_correlation"] is False
