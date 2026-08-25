#!/usr/bin/env python3
"""Deterministic smoking lifecycle replay plus fixture-acceptance preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

APP_ROOT = Path(__file__).resolve().parents[2] / "apps"
if str(APP_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(APP_ROOT / "src"))

from domain.smoking_events import SmokingEpisodeStore, SmokingObservation  # noqa: E402


class ReplayEvidence:
    worker_epoch = "replay-worker"

    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.records: list[tuple[str, str]] = []
        self.ends: list[str] = []

    def start_event(self, **payload: Any) -> str:
        self.starts.append(payload)
        return str(payload["event_id"])

    def record(self, event_id: str, operation: str, _payload: dict[str, Any], **_: Any) -> bool:
        self.records.append((event_id, operation))
        return True

    def finish_event(self, event_id: str, **_: Any) -> None:
        self.ends.append(event_id)


def _config() -> dict[str, Any]:
    return {
        "input": {"camera": "replay"},
        "smoking_behavior": {
            "enabled": True,
            "smoking_threshold": 0.60,
            "temporal": {
                "confirmation_hits": 2,
                "confirmation_window": 4,
                "minimum_duration_seconds": 0.4,
                "clear_negative_observations": 4,
            },
            "lifecycle": {
                "candidate_timeout_seconds": 3.0,
                "clearing_seconds": 3.0,
                "notification_min_duration_seconds": 3.0,
                "trace_interval_ms": 400,
            },
        },
    }


def _score(track_id: int, value: float) -> SmokingObservation:
    return SmokingObservation(track_id, value, (10.0, 10.0, 50.0, 90.0), (2.0, 0.0, 58.0, 100.0))


def _observe(
    store: SmokingEpisodeStore,
    timestamp: float,
    values: dict[int, float],
    observed: set[int] | None = None,
) -> list[Any]:
    observations = [_score(track_id, value) for track_id, value in sorted(values.items())]
    return store.observe(
        frame_num=round(timestamp * 10),
        timestamp=timestamp,
        observations=observations,
        observed_track_ids=set(values) if observed is None else observed,
        frame=np.full((100, 100, 3), 80, dtype=np.uint8),
    )


def run_contract_replay() -> dict[str, Any]:
    evidence = ReplayEvidence()
    store = SmokingEpisodeStore(_config(), evidence)  # type: ignore[arg-type]
    candidate_at = 1.0
    _observe(store, candidate_at, {1: 0.82, 2: 0.20})
    candidate_was_hidden = not store.visible_detections and not evidence.starts
    starts = _observe(store, 1.4, {1: 0.77, 2: 0.90})
    starts += _observe(store, 1.8, {1: 0.75, 2: 0.10})
    starts += _observe(store, 2.2, {1: 0.74, 2: 0.10})
    starts += _observe(store, 2.6, {1: 0.73, 2: 0.10})
    notify = _observe(store, 4.0, {1: 0.71, 2: 0.10})
    _observe(store, 4.4, {}, observed=set())
    _observe(store, 5.0, {1: 0.79})
    active_after_reacquire = list(store.active_event_ids)
    _observe(store, 5.4, {}, observed=set())
    ended = _observe(store, 8.5, {}, observed=set())

    start_rows = [item for item in starts if item.operation == "START"]
    notify_rows = [item for item in notify if item.operation == "NOTIFY"]
    end_rows = [item for item in ended if item.operation == "END"]
    latency = start_rows[0].timestamp - candidate_at if start_rows else None
    gates = {
        "true_positive_start_within_3_seconds": latency is not None and latency <= 3.0,
        "hard_negative_person_did_not_start": all(item.person_track_id != 2 for item in start_rows),
        "candidate_not_visible_before_confirmation": candidate_was_hidden,
        "notification_emitted_once_after_3_seconds": len(notify_rows) == 1
        and sum(operation == "NOTIFY" for _, operation in evidence.records) == 1,
        "reacquire_kept_same_event": bool(start_rows)
        and active_after_reacquire == [start_rows[0].event_id],
        "end_after_clearing_grace": len(end_rows) == 1,
    }
    return {
        "accepted": all(gates.values()),
        "gates": gates,
        "confirmation_latency_seconds": latency,
        "starts": len(start_rows),
        "notifications": len(notify_rows),
        "ends": len(end_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--markup",
        type=Path,
        default=Path("assets/fixtures/mock_videos/smoker/markup.json"),
    )
    args = parser.parse_args()
    contract = run_contract_replay()
    annotation_classes: set[str] = set()
    if args.markup.is_file():
        markup = json.loads(args.markup.read_text(encoding="utf-8"))
        annotation_classes = {
            str(obj.get("class", "")).lower()
            for frames in markup.values()
            for frame in frames
            for obj in frame.get("objects", [])
        }
    fixture_measured = "smoking" in annotation_classes
    report = {
        "accepted": bool(contract["accepted"] and fixture_measured),
        "contract_replay": contract,
        "camera_fixture_acceptance": {
            "measured": fixture_measured,
            "annotation_classes": sorted(annotation_classes),
            "reason": None
            if fixture_measured
            else "fixture markup contains fire/smoke only; no smoking behavior ground truth",
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
