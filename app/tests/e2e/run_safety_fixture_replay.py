#!/usr/bin/env python3
"""Replay annotated fire/smoke fixtures and emit a comparable JSON report."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import yaml

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "app" / "src"))

from adapters.models.fire_smoke_engine import FireSmokeEngine  # noqa: E402
from application.safety_replay import compare_with_baseline, score_presence  # noqa: E402
from bootstrap.config import load_config  # noqa: E402
from domain.fire_smoke_events import FireSmokeEventStore  # noqa: E402


class _ReplayEvidence:
    """In-memory event sink; replay evaluates policy without writing snapshots."""

    worker_epoch = "replay"

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}

    def start_event(self, *, event_id: str, **payload: Any) -> str:
        self.events[event_id] = dict(payload)
        return event_id

    def record(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def finish_event(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _load_manifest(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    annotation_path = ROOT / str(manifest.get("annotation_file", ""))
    if not annotation_path.is_file():
        raise FileNotFoundError(f"annotation file not found: {annotation_path}")
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    cases = manifest.get("cases", []) or []
    if not cases:
        raise ValueError("safety replay manifest must define cases")
    for case in cases:
        video_path = ROOT / str(case.get("video", ""))
        annotation_key = str(case.get("annotation_key", ""))
        if not video_path.is_file():
            raise FileNotFoundError(f"fixture video not found: {video_path}")
        if annotation_key not in annotations:
            raise ValueError(f"annotation key not found: {annotation_key}")
    return manifest, annotations


def _select_rows(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return rows
    # Region confirmation is temporal, so the replay must preserve adjacent
    # inference samples. Pick the most relevant contiguous window instead of
    # spreading samples many seconds apart across a long recording.
    weights = [int(bool(row.get("objects"))) for row in rows]
    window_score = sum(weights[:maximum])
    best_score = window_score
    best_start = 0
    for start in range(1, len(rows) - maximum + 1):
        window_score += weights[start + maximum - 1] - weights[start - 1]
        if window_score > best_score:
            best_score = window_score
            best_start = start
    return rows[best_start : best_start + maximum]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def run_replay(
    manifest: dict[str, Any],
    annotations: dict[str, Any],
    config_path: Path,
    camera_id: str,
    provider: str | None = None,
    model_path: Path | None = None,
) -> dict[str, Any]:
    config = load_config(config_path, camera_id)
    if provider:
        config["fire_smoke"]["providers"] = [provider]
        config["fire_smoke"]["require_gpu_provider"] = provider != "CPUExecutionProvider"
    if model_path:
        config["fire_smoke"]["onnx_path"] = str(model_path.resolve())
    engine = FireSmokeEngine(config)
    maximum = int((manifest.get("sampling", {}) or {}).get("max_frames_per_video", 60))
    scored_rows: list[tuple[set[str], set[str]]] = []
    verified_rows: list[tuple[set[str], set[str]]] = []
    latencies: list[float] = []
    confirmation_latencies: list[float] = []
    false_start_count = 0
    confirmed_start_count = 0
    notification_count = 0
    false_notification_count = 0
    notification_latencies: list[float] = []
    notified_event_ids: list[str] = []
    total_video_seconds = 0.0
    negative_video_seconds = 0.0
    case_reports: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        case_id = str(case["id"])
        selected = _select_rows(list(annotations[str(case["annotation_key"])]), maximum)
        capture = cv2.VideoCapture(str(ROOT / str(case["video"])))
        if not capture.isOpened():
            raise RuntimeError(f"unable to open fixture: {case['video']}")
        case_rows: list[tuple[set[str], set[str]]] = []
        case_verified_rows: list[tuple[set[str], set[str]]] = []
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 25.0)
        video_seconds = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0) / max(fps, 1.0)
        total_video_seconds += video_seconds
        if not any(
            row.get("objects")
            for row in annotations[str(case["annotation_key"])]
        ):
            negative_video_seconds += video_seconds
        replay_evidence = _ReplayEvidence()
        events = FireSmokeEventStore(config, replay_evidence)  # type: ignore[arg-type]
        case_starts: list[dict[str, Any]] = []
        case_notifications: list[dict[str, Any]] = []
        try:
            engine._smoothed.clear()
            engine._cached_detections = []
            engine._last_nonempty_at = 0.0
            for annotation in selected:
                frame_number = int(annotation["frame_num"])
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError(
                        f"unable to read {case_id} frame {frame_number}"
                    )
                started = time.perf_counter()
                detections = engine.infer(frame)
                latencies.append(time.perf_counter() - started)
                expected = {
                    str(item["class"])
                    for item in annotation.get("objects", [])
                    if str(item.get("class")) in {"fire", "smoke"}
                }
                predicted = {str(item.label) for item in detections}
                timestamp = frame_number / max(fps, 1.0)
                transitions = events.observe(
                    frame_num=frame_number,
                    timestamp=timestamp,
                    detections=detections,
                    frame=frame,
                )
                verified = {str(item.label) for item in events.visible_detections}
                # Annotation rows are five seconds apart in these fixtures,
                # while production infers every 300 ms. Replay a bounded
                # adjacent burst after a raw candidate so the temporal gate is
                # measured at runtime cadence instead of annotation cadence.
                needed_labels = predicted - verified
                cadence_frames = max(
                    1,
                    int(round(fps * float(engine.interval_seconds))),
                )
                horizon_frames = max(cadence_frames, int(math.ceil(fps * 3.0)))
                for offset in range(cadence_frames, horizon_frames + 1, cadence_frames):
                    if not needed_labels:
                        break
                    burst_number = frame_number + offset
                    capture.set(cv2.CAP_PROP_POS_FRAMES, burst_number)
                    burst_ok, burst_frame = capture.read()
                    if not burst_ok or burst_frame is None:
                        break
                    burst_started = time.perf_counter()
                    burst_detections = engine.infer(burst_frame)
                    latencies.append(time.perf_counter() - burst_started)
                    transitions.extend(
                        events.observe(
                            frame_num=burst_number,
                            timestamp=burst_number / max(fps, 1.0),
                            detections=burst_detections,
                            frame=burst_frame,
                        )
                    )
                    needed_labels -= {
                        str(item.label) for item in events.visible_detections
                    }
                for transition in transitions:
                    latency = float(transition.confirmation_latency_seconds or 0.0)
                    is_expected = transition.label in expected
                    if transition.operation == "START":
                        confirmed_start_count += 1
                        confirmation_latencies.append(latency)
                        false_start_count += int(not is_expected)
                        case_starts.append(
                            {
                                "event_id": transition.event_id,
                                "label": transition.label,
                                "frame": transition.frame_num,
                                "latency_seconds": round(latency, 4),
                                "expected_at_start": is_expected,
                            }
                        )
                    elif transition.operation == "NOTIFY":
                        notification_count += 1
                        notification_latencies.append(latency)
                        notified_event_ids.append(f"{case_id}|{transition.event_id}")
                        false_notification_count += int(not is_expected)
                        case_notifications.append(
                            {
                                "event_id": transition.event_id,
                                "label": transition.label,
                                "frame": transition.frame_num,
                                "latency_seconds": round(latency, 4),
                                "expected_at_notification": is_expected,
                            }
                        )
                row = (expected, predicted)
                case_rows.append(row)
                scored_rows.append(row)
                verified_row = (expected, verified)
                case_verified_rows.append(verified_row)
                verified_rows.append(verified_row)
        finally:
            capture.release()
        case_reports.append(
            {
                "id": case_id,
                "raw": score_presence(case_rows),
                "verified": score_presence(case_verified_rows),
                "starts": case_starts,
                "notifications": case_notifications,
                "raw_labels": sorted(
                    set().union(*(predicted for _expected, predicted in case_rows))
                ),
                "region_metrics": events.metrics(),
            }
        )
    metrics = score_presence(scored_rows)
    metrics["inference_p50_seconds"] = statistics.median(latencies) if latencies else None
    metrics["inference_p95_seconds"] = _percentile(latencies, 0.95)
    verified_metrics = score_presence(verified_rows)
    case_expected_labels = {
        str(case["id"]): {
            str(item.get("class"))
            for row in annotations[str(case["annotation_key"])]
            for item in row.get("objects", [])
            if str(item.get("class")) in {"fire", "smoke"}
        }
        for case in manifest["cases"]
    }
    expected_fire_case_ids = {
        str(case["id"])
        for case in manifest["cases"]
        if any(
            str(item.get("class")) == "fire"
            for row in annotations[str(case["annotation_key"])]
            for item in row.get("objects", [])
        )
    }
    fire_latency_gate = all(
        bool(
            relevant_starts := [
                start
                for start in report["starts"]
                if start["label"] == "fire" and start["expected_at_start"]
            ]
        )
        and all(start["latency_seconds"] <= 3.0 for start in relevant_starts)
        for report in case_reports
        if report["id"] in expected_fire_case_ids
    )
    region_gates = {
        "true_fire_latency_le_3s": fire_latency_gate,
        "raw_recall_not_decreased": all(
            not (
                label in case_expected_labels[report["id"]]
                and label in report["raw_labels"]
                and not any(start["label"] == label for start in report["starts"])
            )
            for report in case_reports
            for label in ("fire", "smoke")
        ),
        "hard_negative_confirmed_starts_zero": false_start_count == 0,
        "hard_negative_notifications_zero": false_notification_count == 0,
        "notification_delay_at_least_configured": all(
            latency
            >= float(config["fire_smoke"]["tracking"]["notification_min_duration_seconds"])
            for latency in notification_latencies
        ),
        "notification_once_per_event": len(notified_event_ids)
        == len(set(notified_event_ids)),
        "negative_duration_at_least_2_camera_hours": negative_video_seconds >= 7200.0,
    }
    return {
        "measurement_valid": True,
        "camera_id": camera_id,
        "providers": engine.active_providers,
        "model": str(config["fire_smoke"]["onnx_path"]),
        "metrics": metrics,
        "region_verification": {
            "metrics": verified_metrics,
            "confirmed_start_count": confirmed_start_count,
            "false_start_count": false_start_count,
            "confirmation_latency_p95_seconds": _percentile(confirmation_latencies, 0.95),
            "notification_count": notification_count,
            "false_notification_count": false_notification_count,
            "notification_latency_p95_seconds": _percentile(notification_latencies, 0.95),
            "source_video_hours": total_video_seconds / 3600.0,
            "negative_video_hours": negative_video_seconds / 3600.0,
            "gates": region_gates,
            "accepted": all(region_gates.values()),
        },
        "cases": case_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("app/tests/fixtures/safety_ground_truth.yaml"),
    )
    parser.add_argument("--config", type=Path, default=Path("app/config/dev.yaml"))
    parser.add_argument("--camera", default="camera_safety")
    parser.add_argument("--provider")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--report", type=Path, default=Path(".tmp/safety-replay/summary.json"))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    manifest, annotations = _load_manifest(args.manifest)
    if args.validate_only:
        print(json.dumps({"valid": True, "cases": len(manifest["cases"])}))
        return 0
    report = run_replay(
        manifest,
        annotations,
        args.config,
        args.camera,
        provider=args.provider,
        model_path=args.model,
    )
    if args.baseline:
        baseline_report = json.loads(args.baseline.read_text(encoding="utf-8"))
        gates = compare_with_baseline(report["metrics"], baseline_report["metrics"])
        report["gates"] = gates
        report["accepted"] = all(gates.values()) and bool(
            report["region_verification"]["accepted"]
        )
    else:
        report["gates"] = {}
        report["accepted"] = bool(report["region_verification"]["accepted"])
        report["note"] = (
            "Region acceptance was evaluated; raw comparative gates require --baseline."
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"report": str(args.report), "accepted": report["accepted"]}))
    return 0 if report["accepted"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
