"""Aggregate exactly three Platform runtime evidence reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def aggregate(
    summaries: list[dict[str, Any]], *, enforce_gates: bool = False
) -> dict[str, Any]:
    if len(summaries) != 3:
        raise ValueError("The runtime report requires exactly three summaries")
    runs = []
    for index, summary in enumerate(summaries, start=1):
        failed = [name for name, value in summary.get("gates", {}).items() if not value]
        recognition = summary.get("runtime", {}).get("recognition", {})
        runs.append(
            {
                "run": index,
                "accepted": (
                    bool(summary.get("accepted")) if enforce_gates else None
                ),
                "acceptance": {
                    "mode": "gated" if enforce_gates else "evidence_only",
                    "status": (
                        "accepted"
                        if enforce_gates and summary.get("accepted")
                        else "rejected"
                        if enforce_gates
                        else "not_scored"
                    ),
                    "criteria": sorted(summary.get("gates", {}))
                    if enforce_gates
                    else [],
                },
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
                "recognition_cleanup_zero": recognition.get("cleanup_zero"),
                "recognition_writer_drops": recognition.get("writer_drops"),
                "recognition_writer_errors": recognition.get("writer_errors"),
                "measurement": summary.get("measurement", {}),
                "source_hash": summary.get("source_hash", {}),
                "passages": {
                    "face": summary.get("face", {}).get("passages", []),
                    "lpr": summary.get("lpr", {}).get("passages", []),
                },
                "runtime_evidence": summary.get("runtime", {})
                .get("lpr_evidence", {})
                .get("invocation_summaries", []),
                "hardware": {
                    "resources": summary.get("runtime", {}).get("resources", {}),
                    "samples": summary.get("runtime", {}).get(
                        "hardware_samples", {}
                    ),
                },
                "diagnostic_gates": summary.get(
                    "diagnostic_gates", summary.get("gates", {})
                ),
                "artifacts": summary.get("report", {}).get("artifacts", []),
            }
        )
    def numeric(field: str) -> list[Any]:
        return [row[field] for row in runs if row[field] is not None]

    def meets_lpr_target(row: dict[str, Any]) -> bool:
        value = row.get("lpr_exact_match")
        return isinstance(value, int | float) and float(value) >= 2 / 3

    def passages_complete(row: dict[str, Any]) -> bool:
        passages = row.get("passages")
        return (
            isinstance(passages, dict)
            and len(passages.get("lpr", [])) == 11
            and bool(passages.get("face"))
        )

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
            "recognition_cleanup_zero": all(
                row.get("recognition_cleanup_zero") is True for row in runs
            ),
            "recognition_writer_drops": sum(
                numeric("recognition_writer_drops")
            ),
            "recognition_writer_errors": sum(
                numeric("recognition_writer_errors")
            ),
        },
        "mode": "gated" if enforce_gates else "evidence_only",
        "criteria": [],
        "report_complete": all(
            passages_complete(row)
            and row.get("seconds") is not None
            and bool(row.get("source_hash"))
            for row in runs
        ),
        "three_consecutive_hard_gate": (
            all(row["accepted"] for row in runs) if enforce_gates else None
        ),
        "lpr_improvement_target": (
            all(meets_lpr_target(row) for row in runs) if enforce_gates else None
        ),
    }
    result["accepted"] = (
        result["three_consecutive_hard_gate"] if enforce_gates else None
    )
    result["acceptance"] = {
        "mode": result["mode"],
        "status": (
            "accepted"
            if enforce_gates and result["accepted"]
            else "rejected"
            if enforce_gates
            else "not_scored"
        ),
        "criteria": [],
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summaries", nargs=3, type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summaries = [
        json.loads(path.read_text(encoding="utf-8")) for path in args.summaries
    ]
    result = aggregate(summaries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
