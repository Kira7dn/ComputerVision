"""Strict two-camera passage acceptance entrypoint."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import cv2
import numpy as np
import yaml

from passage_metrics import bbox_iou, normalize_plate, percentile
from prepare_passage_fixture import load_manifest

CAMERAS = {"face": "face_camera", "lpr": "car_camera"}
TRACE_CONTAINER_PATH = "/config/passage-trace.jsonl"
LEAD_SECONDS = 1.5
ROUNDS = 3


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_deploy(command: str, config: Path | None = None, timeout: int = 45) -> None:
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "deploy/run.ps1", command]
    if config is not None:
        args += ["-ConfigFile", str(config)]
    subprocess.run(args, check=True, timeout=timeout)


def docker_output(*args: str, timeout: int = 10, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return result.stdout.strip()


def restart_counts() -> dict[str, int]:
    names = [
        name for name in docker_output("ps", "--format", "{{.Names}}").splitlines()
        if name == "frigate" or name.startswith("camera-replay-")
    ]
    return {
        name: int(docker_output("inspect", name, "--format", "{{.RestartCount}}"))
        for name in names
    }


def replay_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return float(result.stdout.strip())


def replay_levels(path: Path) -> tuple[float, float]:
    capture = cv2.VideoCapture(str(path))
    values: list[float] = []
    for seconds in (0.2, LEAD_SECONDS + 0.25):
        capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.release()
            raise RuntimeError(f"cannot sample replay levels: {path}")
        values.append(float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()))
    capture.release()
    black, content = values
    if content - black < 15:
        raise RuntimeError(f"black lead is not distinguishable in {path.name}")
    # FFmpeg's limited-range YUV conversion lifts pure black from 0 to about
    # 16 in latest.jpg. Keep the threshold above that conversion level.
    return max(20.0, black + min(12.0, (content - black) * 0.35)), black + max(15.0, (content - black) * 0.55)


def latest_sample(camera: str) -> tuple[float, float] | None:
    try:
        with urlopen(f"http://127.0.0.1:5001/api/{camera}/latest.jpg", timeout=1.5) as response:
            frame_time = float(response.headers["X-Frame-Time"])
            image = cv2.imdecode(np.frombuffer(response.read(), np.uint8), cv2.IMREAD_GRAYSCALE)
        return None if image is None else (float(image.mean()), frame_time)
    except (HTTPError, URLError, TimeoutError, OSError, TypeError, ValueError):
        return None


def wait_acceptance_ready(expected_image: str | None, timeout: float = 32.0) -> None:
    deadline = time.monotonic() + timeout
    last_reason = "stats unavailable"
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:5001/api/stats", timeout=2) as response:
                stats = json.loads(response.read().decode("utf-8"))
            cameras = stats.get("cameras", {})
            camera_status = {
                camera: {
                    "camera_fps": float((cameras.get(camera) or {}).get("camera_fps", 0)),
                    "process_fps": float((cameras.get(camera) or {}).get("process_fps", 0)),
                }
                for camera in CAMERAS.values()
            }
            # latest.jpg is intentionally not a startup gate. It may lag camera
            # stats while the output process creates its first cached frame; the
            # black-to-content anchor observer below already requires and validates
            # latest.jpg for both cameras before accepting any replay round.
            camera_ready = all(
                status["camera_fps"] > 0
                for status in camera_status.values()
            )
            detectors = stats.get("detectors", {})
            detector_ready = bool(detectors) and all(float(value.get("inference_speed", 9999)) < 200 for value in detectors.values())
            face_ready = (stats.get("embeddings") or {}).get("face_recognition") is not None
            image_ready = True
            if expected_image:
                expected_id = docker_output("image", "inspect", expected_image, "--format", "{{.Id}}", timeout=3)
                running_id = docker_output("inspect", "frigate", "--format", "{{.Image}}", timeout=3)
                image_ready = bool(expected_id) and expected_id == running_id
            if camera_ready and detector_ready and face_ready and image_ready:
                return
            last_reason = (
                f"camera={camera_ready} {camera_status}, detector={detector_ready}, "
                f"face={face_ready}, image={image_ready}"
            )
        except Exception as exc:
            last_reason = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"acceptance runtime not ready: {last_reason}")


def restore_mounts_verified(config: Path) -> bool:
    expected = str(config.resolve()).replace("\\", "/").lower()
    expected_suffix = expected[0] + expected[2:] if len(expected) > 2 and expected[1] == ":" else expected
    try:
        if docker_output("inspect", "frigate", "--format", "{{.State.Running}}") != "true":
            return False
        mounts = json.loads(docker_output("inspect", "frigate", "--format", "{{json .Mounts}}"))
        config_mount = next((mount for mount in mounts if mount.get("Destination") == "/config/config.yml"), None)
        return bool(config_mount) and str(config_mount.get("Source", "")).replace("\\", "/").lower().endswith(expected_suffix)
    except Exception:
        return False


def update_anchor_state(state: dict[str, Any], mean: float, black_max: float, content_min: float, observed_at: float) -> None:
    """Advance one black/content anchor state using consecutive observations."""
    # Internal fixture gaps are at most 0.85 s. Require a longer black run so
    # only the 1.5 s loop lead can become a source anchor.
    min_black_seconds = 1.2
    if observed_at <= state.get("last_observed_at", 0.0):
        return
    state["last_observed_at"] = observed_at
    state.setdefault("means", []).append(round(mean, 2))
    state["means"] = state["means"][-30:]
    if state["mode"] == "black":
        if mean <= black_max:
            state.setdefault("black_started_at", observed_at)
        elif mean >= content_min:
            black_started_at = state.pop("black_started_at", None)
            if black_started_at is not None and observed_at - black_started_at >= min_black_seconds:
                state["mode"] = "content"
                state["content_count"] = 1
                state["content_started_at"] = observed_at
        else:
            state.pop("black_started_at", None)
    elif state["mode"] == "content":
        if mean >= content_min:
            state["content_count"] += 1
            if state["content_count"] >= 2:
                state["anchors"].append(state.pop("content_started_at", observed_at))
                state["mode"] = "await_black"
                state["black_count"] = 0
        else:
            state["content_count"] = 0
            if mean <= black_max:
                state["mode"] = "black"
                state["black_started_at"] = observed_at
    elif state["mode"] == "await_black" and mean <= black_max:
        state["mode"] = "black"
        state["black_started_at"] = observed_at


def parse_bytes(value: str) -> int:
    match = re.match(r"\s*([0-9.]+)\s*([KMGTP]?i?B)", value, re.I)
    if not match:
        raise ValueError(value)
    units = {"B": 1, "KB": 1000, "KIB": 1024, "MB": 1000**2, "MIB": 1024**2,
             "GB": 1000**3, "GIB": 1024**3, "TB": 1000**4, "TIB": 1024**4}
    return int(float(match.group(1)) * units[match.group(2).upper()])


class ResourceSampler:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.memory_bytes: list[int] = []
        self.shm_percent: list[float] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                usage = docker_output("stats", "--no-stream", "--format", "{{.MemUsage}}", "frigate", timeout=5)
                self.memory_bytes.append(parse_bytes(usage.split("/")[0]))
                line = docker_output("exec", "frigate", "sh", "-c", "df -P /dev/shm | tail -1", timeout=5)
                percent = next((token for token in line.split() if token.endswith("%")), None)
                if percent:
                    self.shm_percent.append(float(percent.rstrip("%")))
            except Exception:
                pass
            self.stop_event.wait(1.0)


def observe_round_anchors(replays: dict[str, Path], hard_deadline: float) -> tuple[dict[str, list[float]], dict[str, Any]]:
    thresholds = {CAMERAS[kind]: replay_levels(path) for kind, path in replays.items()}
    durations = {CAMERAS[kind]: replay_duration(path) for kind, path in replays.items()}
    # acceptance-start has just recreated both replay publishers. Restarting
    # only the publishers a second time leaves Frigate/go2rtc subscribed to a
    # stale RTSP session and freezes latest.jpg on the final pre-restart frame.
    # Wait for the next observed black -> content transition instead; this is
    # the source anchor and deliberately does not infer phase from StartedAt.

    states = {
        camera: {"mode": "black", "black_count": 0, "content_count": 0, "anchors": [], "means": []}
        for camera in thresholds
    }
    completion_deadline: float | None = None
    while time.monotonic() < hard_deadline:
        for camera, (black_max, content_min) in thresholds.items():
            sample = latest_sample(camera)
            if sample is None:
                continue
            mean, frame_time = sample
            state = states[camera]
            update_anchor_state(state, mean, black_max, content_min, frame_time)

        if all(len(state["anchors"]) >= ROUNDS for state in states.values()):
            if completion_deadline is None:
                completion_deadline = max(
                    states[camera]["anchors"][ROUNDS - 1] + durations[camera] - LEAD_SECONDS + 0.8
                    for camera in states
                )
            if time.time() >= completion_deadline:
                break
        time.sleep(0.12)

    anchors = {camera: state["anchors"][:ROUNDS] for camera, state in states.items()}
    details = {
        camera: {
            "count": len(anchors[camera]),
            "black_max": round(thresholds[camera][0], 2),
            "content_min": round(thresholds[camera][1], 2),
            "recent_means": state["means"],
        }
        for camera, state in states.items()
    }
    return anchors, details


def merge_passages(manifest: dict[str, Any], fixture: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    windows = {window["id"]: window for window in fixture["replay_windows"][kind]}
    return [{**passage, **windows[passage["id"]]} for passage in manifest[kind]["passages"]]


def record_box(record: dict[str, Any]) -> list[float] | None:
    value = record.get("person_box") or record.get("object_box")
    return [float(v) for v in value] if value is not None else None


def assign_records(
    records: list[dict[str, Any]], anchors: dict[str, list[float]], durations: dict[str, float],
    passages_by_camera: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    track_map: dict[tuple[str, int, str], str] = {}
    for record in records:
        camera = str(record.get("camera", ""))
        frame_time = record.get("frame_time")
        if camera not in anchors or frame_time is None:
            continue
        candidates_round = []
        for index, anchor in enumerate(anchors[camera], 1):
            source_time = LEAD_SECONDS + float(frame_time) - anchor
            if 0.0 <= source_time <= durations[camera] + 0.5:
                candidates_round.append((abs(min(0.0, source_time - LEAD_SECONDS)), index, source_time))
        if not candidates_round:
            continue
        _, round_id, source_time = min(candidates_round)
        record["round_id"] = round_id
        record["source_time_s"] = round(source_time, 4)
        candidates = [
            passage for passage in passages_by_camera[camera]
            if float(passage["start_s"]) - 0.3 <= source_time <= float(passage["end_s"]) + 0.3
        ]
        box = record_box(record)
        if len(candidates) > 1 and box is not None:
            scored = [(bbox_iou(box, [float(v) for v in p["bbox"]]), p) for p in candidates if p.get("bbox")]
            if scored and max(scored, key=lambda item: item[0])[0] >= 0.03:
                candidates = [max(scored, key=lambda item: item[0])[1]]
        elif len(candidates) == 1 and box is not None and candidates[0].get("bbox"):
            if bbox_iou(box, [float(v) for v in candidates[0]["bbox"]]) < 0.01:
                candidates = []
        if len(candidates) == 1:
            record["passage_id"] = candidates[0]["id"]
            if record.get("track_id"):
                track_map[(camera, round_id, str(record["track_id"]))] = str(candidates[0]["id"])

    for record in records:
        key = (str(record.get("camera", "")), int(record.get("round_id", 0)), str(record.get("track_id", "")))
        if not record.get("passage_id") and key in track_map:
            record["passage_id"] = track_map[key]
    return [record for record in records if record.get("round_id")]


def first_record(records: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    found = [record for record in records if record.get("stage") == stage]
    return min(found, key=lambda value: float(value.get("trace_time", value.get("frame_time", 0)))) if found else None


def face_results(records: list[dict[str, Any]], passages: list[dict[str, Any]], anchors: list[float]) -> tuple[dict[str, Any], list[dict[str, Any]], list[float], list[float], list[float], list[float]]:
    active = [passage for passage in passages if passage.get("valid_passage", True)]
    rows: list[dict[str, Any]] = []
    passage_latencies: list[float] = []
    eligible_latencies: list[float] = []
    first_attempts: list[float] = []
    embeddings: list[float] = []
    for passage in active:
        passage_records = [r for r in records if r.get("passage_id") == passage["id"]]
        rounds = []
        all_predictions: list[str] = []
        detected_rounds = 0
        correct_rounds = 0
        for round_id in range(1, ROUNDS + 1):
            current = [r for r in passage_records if r.get("round_id") == round_id]
            qualified = first_record(current, "first_qualified_face")
            attempt = first_record(current, "first_attempt")
            confirmed = first_record(current, "confirmed_result")
            predictions = [str(r.get("identity") or "unknown") for r in current if r.get("stage") in {"first_attempt", "confirmed_result"}]
            known_confirmed = [str(r.get("identity")) for r in current if r.get("stage") == "confirmed_result" and r.get("identity") != "unknown"]
            expected = str(passage["expected_identity"])
            detected = qualified is not None and attempt is not None
            correct = detected and ((expected == "unknown" and not known_confirmed and "unknown" in predictions) or (expected != "unknown" and expected in known_confirmed))
            detected_rounds += int(detected)
            correct_rounds += int(correct)
            all_predictions.extend(predictions)
            end = confirmed if expected != "unknown" else attempt
            if end and round_id <= len(anchors):
                passage_start = anchors[round_id - 1] + float(passage["start_s"]) - LEAD_SECONDS
                passage_latencies.append((float(end.get("trace_time", end["frame_time"])) - passage_start) * 1000)
            if qualified and end:
                eligible_latencies.append((float(end.get("trace_time", end["frame_time"])) - float(qualified.get("trace_time", qualified["frame_time"]))) * 1000)
            if attempt:
                if attempt.get("first_attempt_ms") is not None:
                    first_attempts.append(float(attempt["first_attempt_ms"]))
                if attempt.get("embedding_ms") is not None:
                    embeddings.append(float(attempt["embedding_ms"]))
            rounds.append({
                "round_id": round_id, "detected": detected, "correct": correct, "predictions": predictions,
                "stages": {stage: (first_record(current, stage) or {}).get("trace_time") for stage in ("detector_hit", "first_qualified_face", "candidate_submitted", "first_attempt", "confirmed_result")},
            })
        rows.append({
            "passage_id": passage["id"], "expected": passage["expected_identity"], "predictions": all_predictions,
            "detected": detected_rounds == ROUNDS, "correct": correct_rounds == ROUNDS,
            "detected_rounds": detected_rounds, "correct_rounds": correct_rounds, "rounds": rounds,
        })
    false_records = [
        r for r in records
        if r.get("stage") in {"first_attempt", "confirmed_result"}
        and (not r.get("passage_id") or not next((p for p in passages if p["id"] == r.get("passage_id") and p.get("valid_passage", True)), None))
    ]
    detected = sum(row["detected"] for row in rows)
    correct = sum(row["correct"] for row in rows)
    result = {
        "passages": rows,
        "detection_recall": detected / len(rows) if rows else 0.0,
        "precision": correct / (detected + len(false_records)) if detected + len(false_records) else 0.0,
        "recall": correct / len(rows) if rows else 0.0,
        "false_passages": len(false_records),
    }
    return result, false_records, passage_latencies, eligible_latencies, first_attempts, embeddings


def lpr_results(records: list[dict[str, Any]], passages: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    active = [passage for passage in passages if passage.get("valid_passage", True)]
    rows = []
    for passage in active:
        events = [r for r in records if r.get("passage_id") == passage["id"] and r.get("stage") == "event_published"]
        by_round = {round_id: [r for r in events if r.get("round_id") == round_id] for round_id in range(1, ROUNDS + 1)}
        plates = [normalize_plate(event.get("plate")) for event in events if event.get("plate")]
        representative = collections.Counter(plates).most_common(1)[0][0] if plates else None
        accepted = {normalize_plate(passage.get("expected_plate")), *(normalize_plate(v) for v in passage.get("accepted_plates", []))} - {""}
        exact = None if not passage["readable"] else representative in accepted
        consistency = max(collections.Counter(plates).values()) / len(plates) if plates else 0.0
        stages = {}
        passage_records = [r for r in records if r.get("passage_id") == passage["id"]]
        for stage in ("detector_hit", "track_seen", "lpr_eligible", "plate_detected", "ocr_result", "event_published"):
            stages[stage] = sum(any(r.get("stage") == stage for r in passage_records if r.get("round_id") == round_id) for round_id in range(1, ROUNDS + 1))
        miss_stage = next((stage for stage in ("detector_hit", "track_seen", "lpr_eligible", "plate_detected", "ocr_result", "event_published") if stages[stage] < ROUNDS), None)
        detected_rounds = stages["track_seen"]
        rows.append({
            "passage_id": passage["id"], "readable": passage["readable"], "plates": plates, "representative": representative,
            "detected": detected_rounds > 0, "detected_rounds": detected_rounds,
            "recognized_rounds": sum(bool(value) for value in by_round.values()),
            "repeatability": detected_rounds / ROUNDS,
            "exact": exact, "consistency": consistency, "committed_score_min": min((float(e.get("score", 0)) for e in events), default=None),
            "funnel": stages, "mismatch_reason": (f"{miss_stage}_miss" if miss_stage else ("ocr_exact_mismatch" if exact is False else None)),
        })
    false_records = [
        r for r in records if r.get("stage") == "event_published"
        and (not r.get("passage_id") or not next((p for p in passages if p["id"] == r.get("passage_id") and p.get("valid_passage", True)), None))
    ]
    readable = [row for row in rows if row["readable"]]
    result = {
        "passages": rows,
        "passage_recall": sum(row["detected"] for row in rows) / len(rows) if rows else 0.0,
        "readable_denominator": len(readable),
        "exact_match": sum(row["exact"] is True for row in readable) / len(readable) if readable else None,
        "consistency_min": min((row["consistency"] for row in rows if row["plates"]), default=None),
        "committed_score_min": min((row["committed_score_min"] for row in rows if row["committed_score_min"] is not None), default=None),
        "false_passages": len(false_records),
    }
    return result, false_records


def correlation_mismatches(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping: dict[tuple[str, int, str, int], set[str]] = collections.defaultdict(set)
    for record in records:
        if record.get("track_id") and record.get("passage_id"):
            key = (str(record.get("camera")), int(record.get("round_id", 0)), str(record["track_id"]), int(record.get("generation") or 0))
            mapping[key].add(str(record["passage_id"]))
    return [{"key": list(key), "passages": sorted(values)} for key, values in mapping.items() if len(values) > 1]


def parse_pending(logs: str) -> int | None:
    values = []
    for line in logs.splitlines():
        if "face_pipeline_metrics" not in line or "pending_count" not in line:
            continue
        try:
            values.append(int(json.loads(line[line.index("{"):]).get("pending_count")))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return values[-1] if values else None


def sqlite_events(ids: list[str], database_path: str) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    code = (
        "import sqlite3,json,sys; c=sqlite3.connect(sys.argv[2]); ids=json.loads(sys.argv[1]); "
        "rows=c.execute('select id,sub_label,data from event where id in (%s)' % ','.join('?'*len(ids)),ids).fetchall(); "
        "print(json.dumps({r[0]:{'sub_label':r[1],'data':json.loads(r[2] or '{}')} for r in rows}))"
    )
    output = docker_output("exec", "frigate", "python3", "-c", code, json.dumps(ids), database_path, timeout=10)
    return json.loads(output or "{}")


def api_event(event_id: str) -> dict[str, Any] | None:
    try:
        with urlopen(f"http://127.0.0.1:5001/api/events/{event_id}", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def api_sqlite_consistency(records: list[dict[str, Any]], database_path: str) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    published = [r for r in records if r.get("stage") in {"event_published", "confirmed_result"} and r.get("track_id")]
    expected = {str(record["track_id"]): record for record in published}
    deadline = time.monotonic() + 2.0
    while True:
        database = sqlite_events(list(expected), database_path)
        mismatches = []
        absent_both = []
        for event_id, record in expected.items():
            api = api_event(event_id)
            db = database.get(event_id)
            if api is None and db is None:
                absent_both.append({"event_id": event_id, "stage": record["stage"]})
                continue
            if api is None or db is None:
                mismatches.append({"event_id": event_id, "reason": "missing_api_or_sqlite"})
                continue
            if record["stage"] == "event_published":
                api_value = normalize_plate((api.get("data") or {}).get("recognized_license_plate"))
                db_value = normalize_plate((db.get("data") or {}).get("recognized_license_plate"))
                expected_value = normalize_plate(record.get("plate"))
            else:
                api_value = str(api.get("sub_label") or (api.get("data") or {}).get("face_snapshot_sub_label") or "")
                db_value = str(db.get("sub_label") or (db.get("data") or {}).get("face_snapshot_sub_label") or "")
                expected_value = str(record.get("identity") or "")
            if api_value != expected_value or db_value != expected_value:
                mismatches.append({"event_id": event_id, "reason": "api_sqlite_value_mismatch", "expected": expected_value, "api": api_value, "sqlite": db_value})
        if not mismatches or time.monotonic() >= deadline:
            return not mismatches, mismatches, absent_both
        time.sleep(0.2)


def save_mismatch_evidence(output: Path, manifest: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mismatch_dir = output / "mismatches"
    mismatch_dir.mkdir(parents=True, exist_ok=True)
    for old in mismatch_dir.iterdir():
        if old.is_file():
            old.unlink()
    face_map = {p["id"]: (p, Path(p["source"])) for p in manifest["face"]["passages"]}
    lpr_source = Path(manifest["lpr"]["source"])
    lpr_map = {p["id"]: (p, lpr_source) for p in manifest["lpr"]["passages"]}
    evidence = []
    for row in rows:
        passage_id = row.get("passage_id")
        target = face_map.get(passage_id) or lpr_map.get(passage_id)
        write_json(mismatch_dir / f"{passage_id or 'unmatched'}.json", row)
        if not target:
            continue
        passage, source = target
        capture = cv2.VideoCapture(str(source))
        capture.set(cv2.CAP_PROP_POS_MSEC, ((float(passage["start_s"]) + float(passage["end_s"])) / 2) * 1000)
        ok, frame = capture.read(); capture.release()
        if not ok or frame is None:
            continue
        box = passage.get("bbox")
        if box:
            x1, y1, x2, y2 = (int(v) for v in box)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
        path = mismatch_dir / f"{passage_id}.jpg"
        cv2.imwrite(str(path), frame)
        evidence.append({"passage_id": passage_id, "path": str(path), "sha256": sha256(path), "bbox": box})
    return evidence


def fixture_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    face = [p for p in manifest["face"]["passages"] if p.get("valid_passage", True)]
    lpr = [p for p in manifest["lpr"]["passages"] if p.get("valid_passage", True)]
    result = {
        "known_face_passages": sum(p.get("expected_identity") not in {None, "unknown"} for p in face),
        "unknown_face_passages": sum(p.get("expected_identity") == "unknown" for p in face),
        "close_follow_pairs": len(manifest["face"].get("close_follow", [])),
        "vehicle_passages": len(lpr),
        "readable_vehicle_passages": sum(bool(p.get("readable")) for p in lpr),
    }
    result["valid"] = result["known_face_passages"] >= 2 and result["unknown_face_passages"] >= 2 and result["close_follow_pairs"] >= 1 and result["vehicle_passages"] >= 5 and result["readable_vehicle_passages"] >= 3
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("deploy/config.yaml"))
    parser.add_argument("--manifest", type=Path, default=Path("tools/fixtures/platform_passage_ground_truth.yaml"))
    parser.add_argument("--output", type=Path, default=Path(".tmp/platform-passage"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--image")
    args = parser.parse_args()

    started = time.monotonic()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"schema_version": 2, "profile": "two-camera-passage-acceptance", "accepted": False, "gates": {}, "timing": {}}
    previous_trace_path = os.environ.get("PASSAGE_TRACE_PATH")
    previous_ready_seconds = os.environ.get("CAMERA_READY_STABLE_SECONDS")
    previous_skip_ready = os.environ.get("CAMERA_SKIP_READY_WAIT")
    runtime_started = False
    sampler: ResourceSampler | None = None
    exit_code = 1
    try:
        phase = time.monotonic()
        manifest = load_manifest(args.manifest, Path.cwd())
        contract = fixture_contract(manifest)
        prepare_args = ["--config", str(args.config), "--manifest", str(args.manifest), "--output", str(output)]
        base_config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        model_path = Path.cwd() / str(base_config["runtime"]["model_path"])
        cached_path = output / "fixture.json"
        cached = json.loads(cached_path.read_text(encoding="utf-8")) if cached_path.is_file() else {}
        cache_valid = (
            cached.get("builder_version") == 8
            and cached.get("manifest_sha256") == sha256(args.manifest)
            and cached.get("base_config_sha256") == sha256(args.config)
            and cached.get("model_sha256") == sha256(model_path)
            and (output / "face-replay.mp4").is_file()
            and (output / "lpr-replay.mp4").is_file()
            and Path(cached.get("enrollment_image", "")).is_file()
        )
        if not cache_valid:
            subprocess.run(["python", "tools/prepare_passage_fixture.py", *prepare_args], check=True, timeout=30)
        fixture = json.loads((output / "fixture.json").read_text(encoding="utf-8"))
        isolated_config = Path(fixture["config"])
        value = yaml.safe_load(isolated_config.read_text(encoding="utf-8"))
        value["runtime"]["image"] = args.image or base_config["runtime"]["image"]
        isolated_config.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")
        fixture["config_sha256"] = sha256(isolated_config); write_json(output / "fixture.json", fixture)
        artifact_dir = output / "media" / "clips" / "artifacts"
        if artifact_dir.is_dir() and artifact_dir.resolve().is_relative_to(output):
            shutil.rmtree(artifact_dir)
        database_dir = output / "media" / "passage"
        if database_dir.is_dir() and database_dir.resolve().is_relative_to(output):
            shutil.rmtree(database_dir)
        database_dir.mkdir(parents=True, exist_ok=True)
        summary["fixture"] = fixture; summary["fixture_contract"] = contract
        summary["timing"]["fixture_seconds"] = round(time.monotonic() - phase, 3)
        if args.prepare_only:
            summary["gates"] = {"fixture_contract": contract["valid"], "composites_under_15": replay_duration(output / "face-replay.mp4") <= 15 and replay_duration(output / "lpr-replay.mp4") <= 15}
            exit_code = 0 if all(summary["gates"].values()) else 1
            return exit_code

        isolated_start_wall = time.time(); phase = time.monotonic()
        os.environ["PASSAGE_TRACE_PATH"] = TRACE_CONTAINER_PATH
        os.environ["CAMERA_READY_STABLE_SECONDS"] = "1"
        os.environ["CAMERA_SKIP_READY_WAIT"] = "1"
        runtime_started = True
        run_deploy("acceptance-start", Path(fixture["config"]), timeout=30)
        wait_acceptance_ready(args.image or str(value["runtime"]["image"]), timeout=40)
        summary["timing"]["isolated_start_seconds"] = round(time.monotonic() - phase, 3)
        observation_wall = time.time()
        subprocess.run(["docker", "exec", "frigate", "rm", "-f", TRACE_CONTAINER_PATH], check=False, timeout=10)
        initial_restarts = restart_counts()
        sampler = ResourceSampler(); sampler.start()

        replays = {"face": output / "face-replay.mp4", "lpr": output / "lpr-replay.mp4"}
        phase = time.monotonic()
        anchors, anchor_details = observe_round_anchors(replays, started + 95)
        summary["timing"]["replay_seconds"] = round(time.monotonic() - phase, 3)
        sampler.stop()
        resource_memory = list(sampler.memory_bytes)
        resource_shm = list(sampler.shm_percent)
        sampler = None
        final_restarts = restart_counts()

        trace_path = output / "runtime-trace.jsonl"
        trace_path.unlink(missing_ok=True)
        subprocess.run(["docker", "cp", f"frigate:{TRACE_CONTAINER_PATH}", str(trace_path)], check=False, timeout=10)
        if not trace_path.is_file():
            raise RuntimeError("Passage runtime trace is missing")
        records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        face_passages = merge_passages(manifest, fixture, "face")
        lpr_passages = merge_passages(manifest, fixture, "lpr")
        durations = {CAMERAS[kind]: replay_duration(path) for kind, path in replays.items()}
        passages_by_camera = {CAMERAS["face"]: face_passages, CAMERAS["lpr"]: lpr_passages}
        records = assign_records(records, anchors, durations, passages_by_camera)
        write_json(output / "runtime-trace.json", {"anchors": anchors, "records": records})

        face, face_false, passage_latency, eligible_latency, first_attempt, embeddings = face_results(records, face_passages, anchors[CAMERAS["face"]])
        lpr, lpr_false = lpr_results(records, lpr_passages)
        correlation = correlation_mismatches(records)
        runtime_logs = docker_output("logs", "frigate", "--since", str(int(observation_wall)), timeout=15, check=False)
        model_logs = docker_output("logs", "frigate", "--since", str(int(isolated_start_wall)), timeout=15, check=False)
        pending = parse_pending(runtime_logs)
        bad_log_lines = [line for line in runtime_logs.splitlines() if re.search(r"reconnect|stall|no frames|ffmpeg.*(?:exited|error)", line, re.I)]
        api_consistent, api_mismatches, uncommitted_updates = api_sqlite_consistency(records, str(value["database"]["path"]))
        restart_delta = max((final_restarts.get(name, 0) - initial_restarts.get(name, 0) for name in set(initial_restarts) | set(final_restarts)), default=0)
        detector_count = len(yaml.safe_load(Path(fixture["config"]).read_text(encoding="utf-8")).get("detectors", {}))
        model_loads = len(re.findall(r"ONNX: loading .*yolov9-t-320\.onnx", model_logs))
        face_model_loads = len(re.findall(r"Face recognition initialized", model_logs))

        latency = {
            "face": {
                "passage_to_confirmed_ms_p95": percentile(passage_latency, 95),
                "eligible_to_confirmed_ms_p95": percentile(eligible_latency, 95),
                "first_attempt_ms_p95": percentile(first_attempt, 95),
                "embedding_ms_p95": percentile(embeddings, 95),
            }
        }
        resources = {
            "ram_max_bytes": max(resource_memory, default=None),
            "shm_max_percent": max(resource_shm, default=None),
        }
        if resources["ram_max_bytes"] is None:
            usage = docker_output("stats", "--no-stream", "--format", "{{.MemUsage}}", "frigate", timeout=5)
            resources["ram_max_bytes"] = parse_bytes(usage.split("/")[0])
            line = docker_output("exec", "frigate", "sh", "-c", "df -P /dev/shm | tail -1", timeout=5)
            resources["shm_max_percent"] = float(next(token for token in line.split() if token.endswith("%")).rstrip("%"))
        runtime = {
            "anchors": anchor_details, "rounds_complete": all(len(value) == ROUNDS for value in anchors.values()),
            "pending": pending, "restart_delta": restart_delta, "bad_log_lines": bad_log_lines,
            "correlation_mismatches": correlation, "api_sqlite_mismatches": api_mismatches,
            "uncommitted_updates": uncommitted_updates,
            "model_loads": {"detector_expected": detector_count, "detector_actual": model_loads, "face_expected": 1, "face_actual": face_model_loads},
            "resources": resources,
        }
        summary.update({"face": face, "lpr": lpr, "latency": latency, "runtime": runtime, "source_hash": {"manifest": sha256(args.manifest), "config": sha256(Path(fixture["config"])), "model": fixture.get("model_sha256")}})
        write_json(output / "face.json", {**face, "latency": latency["face"]})
        write_json(output / "lpr.json", lpr)

        failed_rows = [row for row in face["passages"] if not row["correct"]] + [row for row in lpr["passages"] if not row["detected"] or row["exact"] is False]
        failed_rows += [{"passage_id": item.get("passage_id"), "mismatch_reason": "false_passage", "record": item} for item in face_false + lpr_false]
        summary["mismatch_evidence"] = save_mismatch_evidence(output, manifest, failed_rows)
        evidence_ok = all(Path(item["path"]).is_file() and sha256(Path(item["path"])) == item["sha256"] for item in summary["mismatch_evidence"])

        gates = {
            "fixture_contract": contract["valid"],
            "anchors_three_rounds": runtime["rounds_complete"],
            "lpr_recall_above_baseline": lpr["passage_recall"] > 0.6,
            "lpr_readable_denominator": lpr["readable_denominator"] >= 3,
            "lpr_exact_match_reported": lpr["exact_match"] is not None,
            "lpr_no_false_passage": lpr["false_passages"] == 0,
            "face_detection_recall": face["detection_recall"] == 1.0,
            "face_precision": face["precision"] == 1.0,
            "face_recall": face["recall"] == 1.0,
            "face_passage_latency": latency["face"]["passage_to_confirmed_ms_p95"] is not None and latency["face"]["passage_to_confirmed_ms_p95"] <= 3000,
            "face_eligible_latency": latency["face"]["eligible_to_confirmed_ms_p95"] is not None and latency["face"]["eligible_to_confirmed_ms_p95"] <= 1500,
            "face_first_attempt": latency["face"]["first_attempt_ms_p95"] is not None and latency["face"]["first_attempt_ms_p95"] <= 750,
            "face_embedding": latency["face"]["embedding_ms_p95"] is not None and latency["face"]["embedding_ms_p95"] <= 200,
            "pending_zero": pending == 0,
            "correlation": not correlation,
            "api_sqlite_consistency": api_consistent,
            "evidence": evidence_ok,
            "restarts": restart_delta == 0,
            "no_reconnect_or_stall": not bad_log_lines,
            "ram": resources["ram_max_bytes"] is not None and resources["ram_max_bytes"] <= 7 * 1024**3,
            "shm": resources["shm_max_percent"] is not None and resources["shm_max_percent"] < 70,
            "model_load_once_per_instance": model_loads == detector_count and face_model_loads == 1,
        }
        summary["gates"] = gates
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary.setdefault("gates", {})["error_free"] = False
    finally:
        if sampler is not None:
            sampler.stop()
        phase = time.monotonic()
        restore_ok = not runtime_started
        if runtime_started:
            try:
                run_deploy("acceptance-restore", args.config, timeout=30)
                restore_ok = restore_mounts_verified(args.config)
            except Exception as exc:
                summary.setdefault("restore_errors", []).append(f"{type(exc).__name__}: {exc}")
        summary["timing"]["restore_seconds"] = round(time.monotonic() - phase, 3)
        summary["timing"]["total_seconds"] = round(time.monotonic() - started, 3)
        summary.setdefault("gates", {})["runtime_restored"] = restore_ok
        summary["gates"]["under_119_seconds"] = summary["timing"]["total_seconds"] < 119
        summary["accepted"] = bool(summary["gates"]) and all(summary["gates"].values()) and "error" not in summary
        if previous_trace_path is None:
            os.environ.pop("PASSAGE_TRACE_PATH", None)
        else:
            os.environ["PASSAGE_TRACE_PATH"] = previous_trace_path
        if previous_ready_seconds is None:
            os.environ.pop("CAMERA_READY_STABLE_SECONDS", None)
        else:
            os.environ["CAMERA_READY_STABLE_SECONDS"] = previous_ready_seconds
        if previous_skip_ready is None:
            os.environ.pop("CAMERA_SKIP_READY_WAIT", None)
        else:
            os.environ["CAMERA_SKIP_READY_WAIT"] = previous_skip_ready
        write_json(output / "summary.json", summary)
        exit_code = 0 if summary["accepted"] else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
