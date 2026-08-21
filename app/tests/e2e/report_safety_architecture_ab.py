#!/usr/bin/env python3
"""Compare Git baseline/current orchestration and raw-model replay reports."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "app" / "src"))

from application.safety_ab import (  # noqa: E402
    architecture_improvements,
    characterize_orchestration,
    compare_reports,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument(
        "--report", type=Path, default=Path(".tmp/safety-replay/architecture-ab.json")
    )
    args = parser.parse_args()

    relative_worker = "app/src/application/camera_worker.py"
    baseline_source = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:{relative_worker}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    candidate_source = (ROOT / relative_worker).read_text(encoding="utf-8")
    baseline_report = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate_report = json.loads(args.candidate.read_text(encoding="utf-8"))

    baseline_contract = characterize_orchestration(baseline_source)
    candidate_contract = characterize_orchestration(candidate_source)
    improvements = architecture_improvements(baseline_contract, candidate_contract)
    raw_model = compare_reports(baseline_report, candidate_report)
    architecture_accepted = all(improvements.values())
    report = {
        "baseline_ref": args.baseline_ref,
        "baseline_orchestration": baseline_contract,
        "candidate_orchestration": candidate_contract,
        "architecture_improvements": improvements,
        "architecture_accepted": architecture_accepted,
        "raw_model": raw_model,
        "quality_improved": raw_model["raw_model_quality_improved"],
        "conclusion": (
            "Architecture behavior improved; raw model quality is unchanged."
            if architecture_accepted and not raw_model["raw_model_quality_improved"]
            else "Review failed architecture gates or measured model deltas."
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(args.report),
                "architecture_accepted": architecture_accepted,
                "quality_improved": report["quality_improved"],
            }
        )
    )
    return 0 if architecture_accepted and raw_model["candidate_no_regression_accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
