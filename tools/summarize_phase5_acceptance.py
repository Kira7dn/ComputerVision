"""Aggregate three Phase 5 quick runs and an optional soak result."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def aggregate(
    summaries: list[dict[str, Any]], soak: dict[str, Any] | None = None
) -> dict[str, Any]:
    if len(summaries) != 3:
        raise ValueError("Phase 5 requires exactly three quick-run summaries")
    runs = []
    for index, summary in enumerate(summaries, start=1):
        failed = [name for name, value in summary.get("gates", {}).items() if not value]
        recognition = summary.get("runtime", {}).get("recognition_lifecycle", {})
        runs.append(
            {
                "run": index,
                "accepted": bool(summary.get("accepted")),
                "seconds": summary.get("timing", {}).get("total_seconds"),
                "failed_gates": failed,
                "lpr_recall": summary.get("lpr", {}).get("passage_recall"),
                "lpr_precision": summary.get("lpr", {}).get("passage_precision"),
                "lpr_recognition_recall": summary.get("lpr", {}).get("recall"),
                "lpr_recognition_precision": summary.get("lpr", {}).get("precision"),
                "lpr_accuracy": summary.get("lpr", {}).get("accuracy"),
                "lpr_exact_match": summary.get("lpr", {}).get("exact_match"),
                "face_accuracy": summary.get("face", {}).get("accuracy"),
                "face_recall": summary.get("face", {}).get("recall"),
                "face_precision": summary.get("face", {}).get("precision"),
                "max_attempts_per_track": recognition.get("max_attempts_per_track"),
                "duplicate_inference": len(recognition.get("duplicate_inference", [])),
                "early_stop_by_task": recognition.get("early_stop_by_task", {}),
            }
        )
    def numeric(field: str) -> list[Any]:
        return [row[field] for row in runs if row[field] is not None]

    soak_ok = soak is not None and bool(soak.get("accepted"))

    def meets_lpr_target(row: dict[str, Any]) -> bool:
        value = row.get("lpr_exact_match")
        return isinstance(value, int | float) and float(value) >= 2 / 3

    result = {
        "schema_version": 2,
        "runs": runs,
        "worst_run": {
            "seconds_max": max(numeric("seconds"), default=None),
            "lpr_recall_min": min(numeric("lpr_recall"), default=None),
            "lpr_precision_min": min(numeric("lpr_precision"), default=None),
            "lpr_accuracy_min": min(numeric("lpr_accuracy"), default=None),
            "lpr_recognition_recall_min": min(
                numeric("lpr_recognition_recall"), default=None
            ),
            "lpr_recognition_precision_min": min(
                numeric("lpr_recognition_precision"), default=None
            ),
            "lpr_exact_match_min": min(numeric("lpr_exact_match"), default=None),
            "face_accuracy_min": min(numeric("face_accuracy"), default=None),
            "face_recall_min": min(numeric("face_recall"), default=None),
            "face_precision_min": min(numeric("face_precision"), default=None),
            "max_attempts_per_track": max(
                numeric("max_attempts_per_track"), default=None
            ),
            "duplicate_inference": sum(numeric("duplicate_inference")),
        },
        "three_consecutive_hard_gate": all(row["accepted"] for row in runs),
        "lpr_improvement_target": all(meets_lpr_target(row) for row in runs),
        "soak": soak,
        "soak_hard_gate": soak_ok,
    }
    result["accepted"] = result["three_consecutive_hard_gate"] and soak_ok
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summaries", nargs=3, type=Path)
    parser.add_argument("--soak-summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summaries = [
        json.loads(path.read_text(encoding="utf-8")) for path in args.summaries
    ]
    soak = (
        json.loads(args.soak_summary.read_text(encoding="utf-8"))
        if args.soak_summary
        else None
    )
    result = aggregate(summaries, soak)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
