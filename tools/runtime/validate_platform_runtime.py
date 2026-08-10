"""Two-camera Platform runtime evidence implementation."""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import cv2
import numpy as np
import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.lib.passage_metrics import bbox_iou, normalize_plate, percentile
from tools.fixtures.prepare_passage_fixture import load_manifest

CAMERAS = {"face": "face_camera", "lpr": "car_camera"}
TRACE_CONTAINER_PATH = "/config/passage-trace.jsonl"
EVIDENCE_CONTAINER_DIR = "/media/frigate/passage-evidence"
CAPTURE_CUTOFF_CONTAINER_PATH = "/tmp/passage-capture-cutoff"
LEAD_SECONDS = 1.5
ROUNDS = 1
MIN_PASSAGE_RATE = 0.8
MAX_SKIPPED_FPS_REGRESSION = 0.1
ACCEPTANCE_RUNTIME_BUDGET_SECONDS = 150.0


def skipped_fps_within_control(
    current: dict[str, float], control: dict[str, float]
) -> bool:
    """Return whether each camera stays within the control regression budget."""
    return all(
        camera in current
        and camera in control
        and float(current[camera])
        <= float(control[camera]) + MAX_SKIPPED_FPS_REGRESSION
        for camera in CAMERAS.values()
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def wait_file_quiescent(
    path: Path, *, timeout: float = 10.0, stable_seconds: float = 1.0
) -> bool:
    """Wait until a runtime writer has stopped changing a manifest."""
    deadline = time.monotonic() + timeout
    last_signature: tuple[int, int] | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if not path.is_file():
            last_signature = None
            stable_since = None
            time.sleep(0.1)
            continue
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        now = time.monotonic()
        if signature != last_signature:
            last_signature = signature
            stable_since = now
        elif stable_since is not None and now - stable_since >= stable_seconds:
            return True
        time.sleep(0.1)
    return False


def validate_runtime_lpr_evidence(
    evidence_dir: Path,
    anchors: dict[str, list[float]],
    durations: dict[str, float],
    passages_by_camera: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load, attribute, and integrity-check acceptance-only LPR evidence."""
    manifest = evidence_dir / "evidence.jsonl"
    if not manifest.is_file():
        return [], {
            "valid": False,
            "reason": "manifest_missing",
            "invocations": 0,
            "artifact_count": 0,
            "artifact_bytes": 0,
            "errors": ["runtime LPR evidence manifest is missing"],
        }

    records = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # This validator covers LPR evidence; Face evidence is recorded in the
    # same manifest but has its own lifecycle contract.
    records = [record for record in records if record.get("pipeline", "lpr") == "lpr"]
    # Attribution mutates matching records in place. Keep unmatched evidence as
    # well so missing anchors can never make runtime artifacts disappear from
    # the integrity report.
    assign_records(records, anchors, durations, passages_by_camera)
    errors: list[str] = []
    artifact_count = 0
    artifact_bytes = 0
    for record in records:
        relative = record.get("artifact_path")
        if record.get("artifact_rejected"):
            errors.append(
                f"{record.get('evidence_id')}:{record.get('stage')}:"
                f"{record['artifact_rejected']}"
            )
        if not relative:
            continue
        target = (evidence_dir / str(relative)).resolve()
        if not target.is_relative_to(evidence_dir.resolve()) or not target.is_file():
            errors.append(f"missing artifact: {relative}")
            continue
        actual_hash = sha256(target)
        if actual_hash != record.get("artifact_sha256"):
            errors.append(f"sha256 mismatch: {relative}")
        actual_bytes = target.stat().st_size
        if actual_bytes != int(record.get("artifact_bytes", -1)):
            errors.append(f"byte size mismatch: {relative}")
        artifact_count += 1
        artifact_bytes += actual_bytes

    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        if record.get("evidence_id"):
            grouped[str(record["evidence_id"])].append(record)

    invocation_summaries: list[dict[str, Any]] = []
    required_base = {
        "invocation",
        "runtime_frame",
        "runtime_frame_object_box",
        "eligibility_decision",
    }
    for evidence_id, invocation in sorted(grouped.items()):
        stages = {str(record.get("stage")) for record in invocation}
        invocation_errors = [
            f"missing stage: {stage}" for stage in sorted(required_base - stages)
        ]
        decision = next(
            (
                record
                for record in reversed(invocation)
                if record.get("stage") == "eligibility_decision"
            ),
            {},
        )
        detector_result = next(
            (
                record
                for record in reversed(invocation)
                if record.get("stage") == "plate_detector_result"
            ),
            {},
        )
        if decision.get("accepted") and decision.get("reason") != "dedicated_lpr":
            for stage in ("car_crop", "plate_detector_input", "plate_detector_result"):
                if stage not in stages:
                    invocation_errors.append(f"missing stage: {stage}")
        if detector_result.get("accepted"):
            for stage in ("plate_crop", "ocr_plate_input", "ocr_result"):
                if stage not in stages:
                    invocation_errors.append(f"missing stage: {stage}")
        ocr_result = next(
            (
                record
                for record in reversed(invocation)
                if record.get("stage") == "ocr_result"
            ),
            {},
        )
        if int(ocr_result.get("text_box_count", 0)) > 0:
            for stage in ("ocr_text_crop", "ocr_recognition_tensor"):
                if stage not in stages:
                    invocation_errors.append(f"missing stage: {stage}")
        errors.extend(f"{evidence_id}:{error}" for error in invocation_errors)
        invocation_summaries.append(
            {
                "evidence_id": evidence_id,
                "camera": invocation[0].get("camera"),
                "track_id": invocation[0].get("track_id"),
                "frame_time": invocation[0].get("frame_time"),
                "source_pts": invocation[0].get(
                    "source_pts", invocation[0].get("frame_time")
                ),
                "passage_id": next(
                    (
                        record.get("passage_id")
                        for record in invocation
                        if record.get("passage_id")
                    ),
                    None,
                ),
                "stages": sorted(stages),
                "eligibility": {
                    "accepted": decision.get("accepted"),
                    "reason": decision.get("reason"),
                },
                "plate_detector": {
                    "accepted": detector_result.get("accepted"),
                    "reason": detector_result.get("reason"),
                    "score": detector_result.get("detector_score"),
                    "box": detector_result.get("frame_plate_box"),
                },
                "ocr": {
                    "accepted": ocr_result.get("accepted"),
                    "reason": ocr_result.get("reason"),
                    "plate": ocr_result.get("plate"),
                    "mean_character_score": ocr_result.get(
                        "mean_character_score"
                    ),
                    "character_scores": ocr_result.get("character_scores"),
                },
                "errors": invocation_errors,
            }
        )

    expected_passage_ids = {
        str(passage["id"])
        for passage in passages_by_camera.get(CAMERAS["lpr"], [])
        if passage.get("valid_passage", True)
    }
    observed_passage_ids = {
        str(item["passage_id"])
        for item in invocation_summaries
        if item.get("camera") == CAMERAS["lpr"] and item.get("passage_id")
    }
    missing_passage_ids = sorted(expected_passage_ids - observed_passage_ids)
    errors.extend(f"missing passage evidence: {value}" for value in missing_passage_ids)

    summary = {
        "valid": bool(grouped) and not errors,
        "reason": None if grouped and not errors else "incomplete_runtime_evidence",
        "invocations": len(grouped),
        "artifact_count": artifact_count,
        "artifact_bytes": artifact_bytes,
        "expected_passages": len(expected_passage_ids),
        "observed_passages": len(observed_passage_ids & expected_passage_ids),
        "missing_passages": missing_passage_ids,
        "errors": errors,
        "invocation_summaries": invocation_summaries,
    }
    return records, summary


def run_deploy(command: str, config: Path | None = None, timeout: int = 45) -> None:
    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "deploy/run.ps1",
        command,
    ]
    if config is not None:
        args += ["-ConfigFile", str(config)]
    subprocess.run(args, check=True, timeout=timeout)


def docker_output(*args: str, timeout: int = 10, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return result.stdout.strip()


def capture_container_diagnostics(output: Path, since: float | None) -> None:
    """Persist acceptance-container evidence before restore replaces it."""
    inspect = subprocess.run(
        ["docker", "inspect", "frigate"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    (output / "container-inspect.json").write_text(
        inspect.stdout or inspect.stderr, encoding="utf-8"
    )
    args = ["docker", "logs", "frigate"]
    if since is not None:
        args += ["--since", str(int(since))]
    logs = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    (output / "container.log").write_text(
        logs.stdout + logs.stderr, encoding="utf-8"
    )


def restart_counts() -> dict[str, int]:
    names = [
        name
        for name in docker_output("ps", "--format", "{{.Names}}").splitlines()
        if name == "frigate" or name.startswith("camera-replay-")
    ]
    return {
        name: int(docker_output("inspect", name, "--format", "{{.RestartCount}}"))
        for name in names
    }


def replay_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
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
    return max(20.0, black + min(12.0, (content - black) * 0.35)), black + max(
        15.0, (content - black) * 0.55
    )


def latest_sample(camera: str) -> tuple[float, float] | None:
    try:
        with urlopen(
            f"http://127.0.0.1:5001/api/{camera}/latest.jpg", timeout=1.5
        ) as response:
            frame_time = float(response.headers["X-Frame-Time"])
            image = cv2.imdecode(
                np.frombuffer(response.read(), np.uint8), cv2.IMREAD_GRAYSCALE
            )
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
                    "camera_fps": float(
                        (cameras.get(camera) or {}).get("camera_fps", 0)
                    ),
                    "process_fps": float(
                        (cameras.get(camera) or {}).get("process_fps", 0)
                    ),
                }
                for camera in CAMERAS.values()
            }
            # latest.jpg is intentionally not a startup gate. It may lag camera
            # stats while the output process creates its first cached frame; the
            # black-to-content anchor observer below already requires and validates
            # latest.jpg for both cameras before accepting any replay round.
            camera_ready = all(
                status["camera_fps"] > 0 for status in camera_status.values()
            )
            detectors = stats.get("detectors", {})
            detector_ready = bool(detectors) and all(
                float(value.get("inference_speed", 9999)) < 200
                for value in detectors.values()
            )
            face_ready = (stats.get("embeddings") or {}).get(
                "face_recognition"
            ) is not None
            image_ready = True
            if expected_image:
                expected_id = docker_output(
                    "image", "inspect", expected_image, "--format", "{{.Id}}", timeout=3
                )
                running_id = docker_output(
                    "inspect", "frigate", "--format", "{{.Image}}", timeout=3
                )
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
    expected_suffix = (
        expected[0] + expected[2:]
        if len(expected) > 2 and expected[1] == ":"
        else expected
    )
    try:
        if (
            docker_output("inspect", "frigate", "--format", "{{.State.Running}}")
            != "true"
        ):
            return False
        mounts = json.loads(
            docker_output("inspect", "frigate", "--format", "{{json .Mounts}}")
        )
        config_mount = next(
            (
                mount
                for mount in mounts
                if mount.get("Destination") == "/config/config.yml"
            ),
            None,
        )
        return bool(config_mount) and str(config_mount.get("Source", "")).replace(
            "\\", "/"
        ).lower().endswith(expected_suffix)
    except Exception:
        return False


def update_anchor_state(
    state: dict[str, Any],
    mean: float,
    black_max: float,
    content_min: float,
    observed_at: float,
) -> None:
    """Advance one black/content anchor state using consecutive observations."""
    # Internal fixture gaps are at most 0.85 s. Require a longer black run so
    # only the 1.5 s loop lead can become a source anchor.
    min_black_seconds = float(
        os.environ.get("PASSAGE_ANCHOR_MIN_BLACK_SECONDS", "1.2")
    )
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
            if (
                black_started_at is not None
                and observed_at - black_started_at >= min_black_seconds
            ):
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
    units = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
    }
    return int(float(match.group(1)) * units[match.group(2).upper()])


class ResourceSampler:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.memory_bytes: list[int] = []
        self.cpu_percent: list[float] = []
        self.gpu_samples: list[dict[str, float]] = []
        self.errors: list[str] = []
        self.shm_percent: list[float] = []
        self.skipped_fps: dict[str, list[float]] = {
            camera: [] for camera in CAMERAS.values()
        }
        self.evidence_bytes: dict[str, list[int]] = {
            camera: [] for camera in CAMERAS.values()
        }
        self.evidence_pinned: list[int] = []
        self.recognition_lifecycle: list[dict[str, int]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                usage = docker_output(
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.CPUPerc}}|{{.MemUsage}}",
                    "frigate",
                    timeout=5,
                )
                cpu_text, memory_text = usage.split("|", 1)
                self.cpu_percent.append(float(cpu_text.rstrip("%")))
                self.memory_bytes.append(parse_bytes(memory_text.split("/")[0]))
                gpu = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
                for line in gpu.stdout.splitlines():
                    values = [value.strip() for value in line.split(",")]
                    if len(values) == 3:
                        self.gpu_samples.append(
                            {
                                "utilization_percent": float(values[0]),
                                "memory_used_mib": float(values[1]),
                                "memory_total_mib": float(values[2]),
                            }
                        )
                line = docker_output(
                    "exec", "frigate", "sh", "-c", "df -P /dev/shm | tail -1", timeout=5
                )
                percent = next(
                    (token for token in line.split() if token.endswith("%")), None
                )
                if percent:
                    self.shm_percent.append(float(percent.rstrip("%")))
                with urlopen("http://127.0.0.1:5001/api/stats", timeout=2) as response:
                    stats = json.loads(response.read().decode("utf-8"))
                for camera in CAMERAS.values():
                    self.skipped_fps[camera].append(
                        float(
                            (stats.get("cameras", {}).get(camera) or {}).get(
                                "skipped_fps", 0.0
                            )
                        )
                    )
                    evidence = (
                        (stats.get("embeddings") or {})
                        .get("evidence", {})
                        .get("cameras", {})
                        .get(camera, {})
                    )
                    self.evidence_bytes[camera].append(int(evidence.get("bytes", 0)))
                embeddings = stats.get("embeddings") or {}
                self.evidence_pinned.append(
                    int((embeddings.get("evidence") or {}).get("pinned", 0))
                )
                self.recognition_lifecycle.append(
                    {
                        str(key): int(value)
                        for key, value in (
                            embeddings.get("recognition_lifecycle") or {}
                        ).items()
                    }
                    | {
                        "quality_top_k_depth": int(
                            (embeddings.get("quality_selector") or {}).get(
                                "top_k_depth", 0
                            )
                        ),
                        "lpr_queue_depth": int(
                            embeddings.get("lpr_queue_depth", 0)
                        ),
                    }
                )
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self.stop_event.wait(1.0)


def wait_recognition_idle(timeout: float = 10.0) -> bool:
    """Allow bounded deferred work to release attempts and evidence leases."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:5001/api/stats", timeout=1) as response:
                stats = json.loads(response.read().decode("utf-8"))
            embeddings = stats.get("embeddings") or {}
            lifecycle = embeddings.get("recognition_lifecycle") or {}
            evidence = embeddings.get("evidence") or {}
            if (
                int(lifecycle.get("in_flight", 0)) == 0
                and int(lifecycle.get("active_lifecycles", 0)) == 0
                and int(lifecycle.get("quality_top_k_depth", 0)) == 0
                and int(lifecycle.get("lpr_queue_depth", 0)) == 0
                and int(evidence.get("pinned", 0)) == 0
            ):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def observe_round_anchors(
    replays: dict[str, Path], hard_deadline: float
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    thresholds = {CAMERAS[kind]: replay_levels(path) for kind, path in replays.items()}
    durations = {CAMERAS[kind]: replay_duration(path) for kind, path in replays.items()}
    # acceptance-start has just recreated both replay publishers. Restarting
    # only the publishers a second time leaves Frigate/go2rtc subscribed to a
    # stale RTSP session and freezes latest.jpg on the final pre-restart frame.
    # Wait for the next observed black -> content transition instead; this is
            # the source anchor and deliberately does not infer source time from StartedAt.

    states: dict[str, dict[str, Any]] = {
        camera: {
            "mode": "black",
            "black_count": 0,
            "content_count": 0,
            "anchors": [],
            "means": [],
        }
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
                    states[camera]["anchors"][ROUNDS - 1]
                    + durations[camera]
                    - LEAD_SECONDS
                    + 0.8
                    for camera in states
                )
            if time.time() >= completion_deadline:
                break
        time.sleep(0.12)

    anchors: dict[str, list[float]] = {
        camera: list(state["anchors"][:ROUNDS]) for camera, state in states.items()
    }
    details: dict[str, Any] = {
        camera: {
            "count": len(anchors[camera]),
            "black_max": round(thresholds[camera][0], 2),
            "content_min": round(thresholds[camera][1], 2),
            "recent_means": state["means"],
        }
        for camera, state in states.items()
    }
    return anchors, details


def merge_passages(
    manifest: dict[str, Any], fixture: dict[str, Any], kind: str
) -> list[dict[str, Any]]:
    windows = {window["id"]: window for window in fixture["replay_windows"][kind]}
    return [
        {**passage, **windows[passage["id"]]} for passage in manifest[kind]["passages"]
    ]


def record_box(record: dict[str, Any]) -> list[float] | None:
    value = record.get("person_box") or record.get("object_box")
    return [float(v) for v in value] if value is not None else None


def assign_records(
    records: list[dict[str, Any]],
    anchors: dict[str, list[float]],
    durations: dict[str, float],
    passages_by_camera: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Assign one runtime track trajectory to at most one physical passage."""
    for record in records:
        # frame_time is the source PTS; expose it explicitly for scoring.
        if record.get("source_pts") is None and record.get("frame_time") is not None:
            record["source_pts"] = record["frame_time"]
        if record.get("passage_id") and not record.get("recognition_passage_id"):
            record["recognition_passage_id"] = record["passage_id"]
        # ``passage_id`` is reserved for the ground-truth passage in this
        # report. Runtime ownership remains available as recognition_passage_id.
        record.pop("passage_id", None)
        camera = str(record.get("camera", ""))
        frame_time = record.get("source_pts")
        if camera not in anchors or frame_time is None:
            continue
        candidates_round = []
        for index, anchor in enumerate(anchors[camera], 1):
            source_time = LEAD_SECONDS + float(frame_time) - anchor
            if 0.0 <= source_time <= durations[camera] + 0.5:
                candidates_round.append(
                    (abs(min(0.0, source_time - LEAD_SECONDS)), index, source_time)
                )
        if not candidates_round:
            continue
        _, round_id, source_time = min(candidates_round)
        record["round_id"] = round_id
        record["source_time_s"] = round(source_time, 4)

    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = collections.defaultdict(
        list
    )
    untracked: list[dict[str, Any]] = []
    for record in records:
        camera = str(record.get("camera", ""))
        round_id = int(record.get("round_id", 0))
        owner_id = record.get("track_id") or record.get("recognition_passage_id")
        if camera and round_id and owner_id:
            grouped[(camera, round_id, str(owner_id))].append(record)
        elif round_id:
            untracked.append(record)

    for (camera, _round_id, _owner_id), trajectory in grouped.items():
        scores: dict[str, float] = {}
        temporal_candidates: set[str] = set()
        scoring_records = trajectory
        for preferred_stage in ("event_published", "ocr_result", "plate_detected"):
            preferred = [
                record
                for record in trajectory
                if record.get("stage") == preferred_stage and record_box(record)
            ]
            if preferred:
                scoring_records = preferred
                break
        for record in scoring_records:
            box = record_box(record)
            source_time = record.get("source_time_s")
            if box is None or source_time is None:
                continue
            for passage in passages_by_camera.get(camera, []):
                if not (
                    float(passage["start_s"]) - 0.3
                    <= float(source_time)
                    <= float(passage["end_s"]) + 0.3
                ):
                    continue
                passage_box = passage.get("bbox")
                if not passage_box:
                    continue
                passage_id = str(passage["id"])
                temporal_candidates.add(passage_id)
                scores[passage_id] = max(
                    scores.get(passage_id, 0.0),
                    bbox_iou(box, [float(value) for value in passage_box]),
                )

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if not ranked:
            continue
        best_id, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        minimum_score = 0.01 if len(temporal_candidates) == 1 else 0.03
        # A clear recognition-trajectory winner owns every record in the
        # runtime track. Earlier detector boxes cannot override later evidence
        # that was actually used for OCR/publish.
        # Comparable strong matches indicate a real track switch/reuse and
        # fail closed instead of splitting one track across physical vehicles.
        if best_score < minimum_score or (
            second_score >= 0.03 and best_score - second_score < 0.05
        ):
            continue
        for record in trajectory:
            record["passage_id"] = best_id
            record["fixture_passage_id"] = best_id

    # Records without runtime lineage cannot inherit a trajectory. Permit only
    # one unambiguous bbox-backed match and never use OCR text for attribution.
    for record in untracked:
        camera = str(record.get("camera", ""))
        source_time = record.get("source_time_s")
        box = record_box(record)
        if source_time is None or box is None:
            continue
        temporal_candidate_count = sum(
            bool(passage.get("bbox"))
            and float(passage["start_s"]) - 0.3
            <= float(source_time)
            <= float(passage["end_s"]) + 0.3
            for passage in passages_by_camera.get(camera, [])
        )
        ranked = sorted(
            (
                (
                    bbox_iou(box, [float(value) for value in passage["bbox"]]),
                    str(passage["id"]),
                )
                for passage in passages_by_camera.get(camera, [])
                if passage.get("bbox")
                and float(passage["start_s"]) - 0.3
                <= float(source_time)
                <= float(passage["end_s"]) + 0.3
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if not ranked:
            continue
        best_score, best_id = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        minimum_score = 0.01 if temporal_candidate_count == 1 else 0.03
        if best_score < minimum_score or (
            second_score >= 0.03 and best_score - second_score < 0.05
        ):
            continue
        record["passage_id"] = best_id
        record["fixture_passage_id"] = best_id
    return [record for record in records if record.get("round_id")]


def first_record(records: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    found = [record for record in records if record.get("stage") == stage]
    return (
        min(
            found,
            key=lambda value: float(value.get("source_pts", value.get("frame_time", 0))),
        )
        if found
        else None
    )


def stage_trace(records: list[dict[str, Any]], stages: tuple[str, ...]) -> dict[str, Any]:
    """Return raw per-stage records plus processing latency between stages."""
    ordered = sorted(
        records,
        key=lambda value: float(value.get("source_pts", value.get("frame_time", 0))),
    )
    first = {stage: first_record(ordered, stage) for stage in stages}
    timestamps = {
        stage: (record or {}).get("trace_time") for stage, record in first.items()
    }
    latency_ms = {}
    for previous, current in zip(stages, stages[1:]):
        previous_time = (first.get(previous) or {}).get("trace_time")
        current_time = (first.get(current) or {}).get("trace_time")
        latency_ms[f"{previous}_to_{current}"] = (
            round((float(current_time) - float(previous_time)) * 1000, 3)
            if previous_time is not None and current_time is not None
            else None
        )
    return {
        "stage_timestamps": timestamps,
        "latency_ms": latency_ms,
        "records": ordered,
    }


def trace_metrics(records: list[dict[str, Any]], elapsed_seconds: float) -> dict[str, Any]:
    """Count every observed pipeline stage and expose normalized calls/s."""
    counts = collections.Counter(str(record.get("stage")) for record in records)
    return {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "stage_counts": dict(sorted(counts.items())),
        "stage_calls_per_second": {
            stage: round(count / elapsed_seconds, 3)
            for stage, count in sorted(counts.items())
            if elapsed_seconds > 0
        },
    }


def source_pts_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe source-PTS completeness and gaps without inventing a gate."""
    result: dict[str, Any] = {}
    for camera in sorted({str(record.get("camera")) for record in records}):
        values = sorted(
            float(record["source_pts"])
            for record in records
            if str(record.get("camera")) == camera and record.get("source_pts") is not None
        )
        gaps = [current - previous for previous, current in zip(values, values[1:])]
        result[camera] = {
            "records_with_source_pts": len(values),
            "max_gap_seconds": round(max(gaps, default=0.0), 6),
            "mean_gap_seconds": round(sum(gaps) / len(gaps), 6) if gaps else None,
            "missing_source_pts": sum(
                1
                for record in records
                if str(record.get("camera")) == camera
                and record.get("source_pts") is None
            ),
        }
    return result


def false_passage_count(records: list[dict[str, Any]]) -> int:
    """Count physical false passages without counting repeated stage updates twice."""
    keys: set[tuple[Any, ...]] = set()
    for index, record in enumerate(records):
        passage_or_track = record.get("passage_id") or record.get("track_id")
        keys.add(
            (
                record.get("camera"),
                record.get("round_id"),
                passage_or_track if passage_or_track is not None else ("record", index),
                record.get("generation", 0),
            )
        )
    return len(keys)


def face_results(
    records: list[dict[str, Any]], passages: list[dict[str, Any]], anchors: list[float]
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[float],
    list[float],
    list[float],
    list[float],
]:
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
            attempt_predictions = [
                str(r.get("identity") or "unknown")
                for r in current
                if r.get("stage") == "first_attempt"
            ]
            known_confirmed = [
                str(r.get("identity"))
                for r in current
                if r.get("stage") == "confirmed_result"
                and r.get("identity") != "unknown"
            ]
            expected = str(passage["expected_identity"])
            detected = qualified is not None and attempt is not None
            correct = detected and (
                (
                    expected == "unknown"
                    and not known_confirmed
                )
                or (expected != "unknown" and expected in known_confirmed)
            )
            detected_rounds += int(detected)
            correct_rounds += int(correct)
            all_predictions.extend(known_confirmed)
            end = confirmed if expected != "unknown" else attempt
            if end and round_id <= len(anchors):
                passage_start = (
                    anchors[round_id - 1] + float(passage["start_s"]) - LEAD_SECONDS
                )
                passage_latencies.append(
                    (float(end.get("trace_time", end["frame_time"])) - passage_start)
                    * 1000
                )
            if qualified and end:
                eligible_latencies.append(
                    (
                        float(end.get("trace_time", end["frame_time"]))
                        - float(qualified.get("trace_time", qualified["frame_time"]))
                    )
                    * 1000
                )
            if attempt:
                if attempt.get("first_attempt_ms") is not None:
                    first_attempts.append(float(attempt["first_attempt_ms"]))
                if attempt.get("embedding_ms") is not None:
                    embeddings.append(float(attempt["embedding_ms"]))
            rounds.append(
                {
                    "round_id": round_id,
                    "detected": detected,
                    "correct": correct,
                    "predictions": known_confirmed,
                    "attempt_predictions": attempt_predictions,
                    "stages": {
                        stage: (first_record(current, stage) or {}).get("trace_time")
                        for stage in (
                            "detector_hit",
                            "first_qualified_face",
                            "candidate_submitted",
                            "first_attempt",
                            "confirmed_result",
                        )
                    },
                    "trace": stage_trace(
                        current,
                        (
                            "detector_hit",
                            "first_qualified_face",
                            "candidate_submitted",
                            "first_attempt",
                            "confirmed_result",
                        ),
                    ),
                }
            )
        rows.append(
            {
                "passage_id": passage["id"],
                "expected": passage["expected_identity"],
                "predictions": all_predictions,
                "detected": detected_rounds >= (ROUNDS + 1) // 2,
                "correct": correct_rounds >= (ROUNDS + 1) // 2,
                "detected_rounds": detected_rounds,
                "correct_rounds": correct_rounds,
                "rounds": rounds,
            }
        )
    false_records = [
        r
        for r in records
        if r.get("stage") == "confirmed_result"
        and (
            not r.get("passage_id")
            or not next(
                (
                    p
                    for p in passages
                    if p["id"] == r.get("passage_id") and p.get("valid_passage", True)
                ),
                None,
            )
        )
    ]
    false_passages = false_passage_count(false_records)
    detected = sum(row["detected"] for row in rows)
    correct = sum(row["correct"] for row in rows)
    exact_published = sum(
        row["expected"] != "unknown"
        and bool(row["predictions"])
        and collections.Counter(row["predictions"]).most_common(1)[0][0]
        == row["expected"]
        for row in rows
    )
    recognition_publishes = (
        sum(bool(row["predictions"]) for row in rows) + false_passages
    )
    result = {
        "passages": rows,
        "accuracy": correct / len(rows) if rows else 0.0,
        "detection_recall": detected / len(rows) if rows else 0.0,
        "precision": exact_published / recognition_publishes
        if recognition_publishes
        else 1.0,
        "recall": correct / len(rows) if rows else 0.0,
        "recognition_publish_count": recognition_publishes,
        "false_passages": false_passages,
    }
    return (
        result,
        false_records,
        passage_latencies,
        eligible_latencies,
        first_attempts,
        embeddings,
    )


def lpr_results(
    records: list[dict[str, Any]], passages: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    active = [passage for passage in passages if passage.get("valid_passage", True)]
    rows = []
    for passage in active:
        events = [
            r
            for r in records
            if r.get("passage_id") == passage["id"]
            and r.get("stage") == "event_published"
        ]
        by_round = {
            round_id: [r for r in events if r.get("round_id") == round_id]
            for round_id in range(1, ROUNDS + 1)
        }
        plates = [
            normalize_plate(event.get("plate"))
            for event in events
            if event.get("plate")
        ]
        representative = (
            collections.Counter(plates).most_common(1)[0][0] if plates else None
        )
        accepted = {
            normalize_plate(passage.get("expected_plate")),
            *(normalize_plate(v) for v in passage.get("accepted_plates", [])),
        } - {""}
        exact = None if not passage["readable"] else representative in accepted
        consistency = (
            max(collections.Counter(plates).values()) / len(plates) if plates else 0.0
        )
        stages = {}
        passage_records = [r for r in records if r.get("passage_id") == passage["id"]]
        for stage in (
            "detector_hit",
            "track_seen",
            "lpr_eligible",
            "plate_detected",
            "ocr_result",
            "event_published",
        ):
            stages[stage] = sum(
                any(
                    r.get("stage") == stage
                    for r in passage_records
                    if r.get("round_id") == round_id
                )
                for round_id in range(1, ROUNDS + 1)
            )
        miss_stage = next(
            (
                stage
                for stage in (
                    "detector_hit",
                    "track_seen",
                    "lpr_eligible",
                    "plate_detected",
                    "ocr_result",
                    "event_published",
                )
                if stages[stage] < ROUNDS
            ),
            None,
        )
        detected_rounds = stages["track_seen"]
        round_trace = {
            round_id: stage_trace(
                [r for r in passage_records if r.get("round_id") == round_id],
                (
                    "detector_hit",
                    "track_seen",
                    "lpr_eligible",
                    "plate_detected",
                    "ocr_result",
                    "event_published",
                ),
            )
            for round_id in range(1, ROUNDS + 1)
        }
        rows.append(
            {
                "passage_id": passage["id"],
                "readable": passage["readable"],
                "plates": plates,
                "representative": representative,
                "detected": detected_rounds >= (ROUNDS + 1) // 2,
                "detected_rounds": detected_rounds,
                "recognized_rounds": sum(bool(value) for value in by_round.values()),
                "repeatability": detected_rounds / ROUNDS,
                "exact": exact,
                "consistency": consistency,
                "committed_score_min": min(
                    (float(e.get("score", 0)) for e in events), default=None
                ),
                "funnel": stages,
                "round_trace": round_trace,
                "mismatch_reason": (
                    f"{miss_stage}_miss"
                    if miss_stage
                    else ("ocr_exact_mismatch" if exact is False else None)
                ),
            }
        )
    false_records = [
        r
        for r in records
        if r.get("stage") == "event_published"
        and (
            not r.get("passage_id")
            or not next(
                (
                    p
                    for p in passages
                    if p["id"] == r.get("passage_id") and p.get("valid_passage", True)
                ),
                None,
            )
        )
    ]
    false_passages = false_passage_count(false_records)
    readable = [row for row in rows if row["readable"]]
    detected = sum(row["detected"] for row in rows)
    exact_tp = sum(row["exact"] is True for row in readable)
    accuracy = (
        exact_tp / len(readable)
        if readable
        else None
    )
    passage_recall = detected / len(rows) if rows else 0.0
    passage_precision = (
        detected / (detected + false_passages)
        if detected + false_passages
        else 0.0
    )
    recognition_publishes = sum(bool(row["plates"]) for row in rows) + false_passages
    recognition_precision = (
        exact_tp / recognition_publishes if recognition_publishes else 1.0
    )
    recognition_recall = exact_tp / len(readable) if readable else 0.0
    result = {
        "passages": rows,
        "accuracy": accuracy,
        "precision": recognition_precision,
        "recall": recognition_recall,
        "passage_recall": passage_recall,
        "passage_precision": passage_precision,
        "recognition_publish_count": recognition_publishes,
        "exact_true_positives": exact_tp,
        "readable_denominator": len(readable),
        "exact_match": accuracy,
        "consistency_min": min(
            (row["consistency"] for row in rows if row["plates"]), default=None
        ),
        "committed_score_min": min(
            (
                row["committed_score_min"]
                for row in rows
                if row["committed_score_min"] is not None
            ),
            default=None,
        ),
        "false_passages": false_passages,
    }
    return result, false_records


def correlation_mismatches(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping: dict[tuple[str, int, str, int], set[str]] = collections.defaultdict(set)
    for record in records:
        if record.get("track_id") and record.get("passage_id"):
            key = (
                str(record.get("camera")),
                int(record.get("round_id", 0)),
                str(record["track_id"]),
                int(record.get("generation") or 0),
            )
            mapping[key].add(str(record["passage_id"]))
    mismatches: list[dict[str, Any]] = [
        {"key": list(key), "passages": sorted(values)}
        for key, values in mapping.items()
        if len(values) > 1
    ]
    seen_candidates: set[tuple[str, str, str]] = set()
    for record in records:
        if record.get("stage") not in {
            "first_attempt",
            "confirmed_result",
            "event_published",
        }:
            continue
        required = ("candidate_id", "source_pts", "quality_score", "quality_components")
        missing = [name for name in required if record.get(name) is None]
        if record.get("source_role") != "detect":
            missing.append("source_role=detect")
        if record.get("stage") == "event_published":
            missing.extend(
                name
                for name in ("plate_box", "evidence_id", "frame_ref")
                if not record.get(name)
            )
        else:
            missing.extend(
                name for name in ("person_box", "face_box") if not record.get(name)
            )
        if missing:
            mismatches.append(
                {
                    "stage": record.get("stage"),
                    "reason": "lineage_missing",
                    "fields": missing,
                }
            )
        identity = (
            str(record.get("stage")),
            str(record.get("track_id")),
            str(record.get("candidate_id")),
        )
        if identity in seen_candidates and record.get("stage") in {
            "confirmed_result",
            "event_published",
        }:
            mismatches.append(
                {
                    "stage": record.get("stage"),
                    "reason": "duplicate_candidate_commit",
                    "candidate_id": identity[2],
                }
            )
        seen_candidates.add(identity)
    return mismatches


def recognition_lifecycle_summary(
    records: list[dict[str, Any]], stats: dict[str, int]
) -> dict[str, Any]:
    attempts = [
        record
        for record in records
        if record.get("stage") == "recognition_attempt"
        and record.get("inference_started", True)
    ]
    groups: dict[tuple[str, str, str, int], list[dict[str, Any]]] = (
        collections.defaultdict(list)
    )
    for record in attempts:
        groups[
            (
                str(record.get("task")),
                str(record.get("camera")),
                str(
                    record.get("recognition_passage_id")
                    or record.get("passage_id")
                    or record.get("track_id")
                ),
                int(record.get("generation") or 0),
            )
        ].append(record)
    duplicate_inference = []
    for key, values in groups.items():
        seen: set[str] = set()
        for value in values:
            candidate_id = str(value.get("candidate_id") or "")
            if candidate_id and candidate_id in seen:
                duplicate_inference.append(
                    {"key": list(key), "candidate_id": candidate_id}
                )
            seen.add(candidate_id)
    terminals = [
        record for record in records if record.get("stage") == "recognition_terminal"
    ]
    early_stop_by_task = {
        task: any(
            record.get("task") == task
            and record.get("status") == "ACCEPTED"
            and len(
                groups.get(
                    (
                        task,
                        str(record.get("camera")),
                        str(
                            record.get("recognition_passage_id")
                            or record.get("passage_id")
                            or record.get("track_id")
                        ),
                        int(record.get("generation") or 0),
                    ),
                    [],
                )
            )
            < 3
            for record in terminals
        )
        for task in ("face", "lpr")
    }
    return {
        "attempts": len(attempts),
        "passages": len(groups),
        "max_attempts_per_track": max(map(len, groups.values()), default=0),
        "attempts_per_track": {
            "min": min(map(len, groups.values()), default=0),
            "max": max(map(len, groups.values()), default=0),
            "mean": round(len(attempts) / len(groups), 3) if groups else 0.0,
        },
        "compute_ms_by_task": {
            task: round(
                sum(
                    float(record.get("latency_ms") or 0)
                    for record in attempts
                    if record.get("task") == task
                ),
                3,
            )
            for task in ("face", "lpr")
        },
        "duplicate_inference": duplicate_inference,
        "terminals": terminals,
        "terminal_reasons": dict(
            collections.Counter(str(record.get("reason")) for record in terminals)
        ),
        "early_stop_by_task": early_stop_by_task,
        "stats": stats,
    }


def parse_pending(logs: str) -> int | None:
    values = []
    for line in logs.splitlines():
        if "face_pipeline_metrics" not in line or "pending_count" not in line:
            continue
        try:
            values.append(int(json.loads(line[line.index("{") :]).get("pending_count")))
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
    output = docker_output(
        "exec",
        "frigate",
        "python3",
        "-c",
        code,
        json.dumps(ids),
        database_path,
        timeout=10,
    )
    return json.loads(output or "{}")


def api_event(event_id: str) -> dict[str, Any] | None:
    try:
        with urlopen(
            f"http://127.0.0.1:5001/api/events/{event_id}", timeout=2
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def api_sqlite_consistency(
    records: list[dict[str, Any]], database_path: str
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    published = [
        r
        for r in records
        if r.get("stage") in {"event_published", "confirmed_result"}
        and r.get("track_id")
    ]
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
                mismatches.append(
                    {"event_id": event_id, "reason": "missing_api_or_sqlite"}
                )
                continue
            if record["stage"] == "event_published":
                api_value = normalize_plate(
                    (api.get("data") or {}).get("recognized_license_plate")
                )
                db_value = normalize_plate(
                    (db.get("data") or {}).get("recognized_license_plate")
                )
                expected_value = normalize_plate(record.get("plate"))
            else:
                api_value = str(
                    api.get("sub_label")
                    or (api.get("data") or {}).get("face_snapshot_sub_label")
                    or ""
                )
                db_value = str(
                    db.get("sub_label")
                    or (db.get("data") or {}).get("face_snapshot_sub_label")
                    or ""
                )
                expected_value = str(record.get("identity") or "")
            if api_value != expected_value or db_value != expected_value:
                mismatches.append(
                    {
                        "event_id": event_id,
                        "reason": "api_sqlite_value_mismatch",
                        "expected": expected_value,
                        "api": api_value,
                        "sqlite": db_value,
                    }
                )
        if not mismatches or time.monotonic() >= deadline:
            return not mismatches, mismatches, absent_both
        time.sleep(0.2)


def fixture_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    face = [p for p in manifest["face"]["passages"] if p.get("valid_passage", True)]
    lpr = [p for p in manifest["lpr"]["passages"] if p.get("valid_passage", True)]
    result = {
        "known_face_passages": sum(
            p.get("expected_identity") not in {None, "unknown"} for p in face
        ),
        "unknown_face_passages": sum(
            p.get("expected_identity") == "unknown" for p in face
        ),
        "close_follow_pairs": len(manifest["face"].get("close_follow", [])),
        "vehicle_passages": len(lpr),
        "readable_vehicle_passages": sum(bool(p.get("readable")) for p in lpr),
    }
    result["valid"] = (
        result["known_face_passages"] >= 2
        and result["unknown_face_passages"] >= 2
        and result["close_follow_pairs"] >= 1
        and result["vehicle_passages"] >= 5
        and result["readable_vehicle_passages"] >= 3
    )
    return result


def _md_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_md_value(value) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines) if rows else "_Không có dữ liệu._"


def write_markdown_report(output: Path, summary: dict[str, Any]) -> Path:
    """Write a self-contained human-readable report for one runtime invocation."""
    runtime = summary.get("runtime", {})
    resources = runtime.get("resources", {})
    gpu = resources.get("gpu", {})
    lpr = summary.get("lpr", {})
    face = summary.get("face", {})
    timing = summary.get("timing", {})
    measurement = summary.get("measurement", {})
    trace = runtime.get("trace_metrics", {})
    lpr_trace = runtime.get("lpr_evidence_trace_metrics", {})
    lines = [
        "# Platform Runtime Test Report",
        "",
        f"- **Run:** `{output.name}`",
        f"- **Report status:** `{summary.get('report', {}).get('status', '-')}`",
        f"- **Acceptance:** `{summary.get('acceptance', {}).get('status', '-')}` (evidence-only)",
        f"- **Measurement valid:** `{measurement.get('measurement_valid', '-')}`",
        "",
        "## 1. Tổng quan runtime",
        "",
        _md_table(
            ["Metric", "Value"],
            [
                ["Total seconds", timing.get("total_seconds")],
                ["Replay seconds", timing.get("replay_seconds")],
                ["Restore seconds", timing.get("restore_seconds")],
                ["Rounds complete", runtime.get("rounds_complete")],
                ["Source PTS complete", measurement.get("source_pts_complete")],
                ["Pending", runtime.get("pending")],
                ["Restart delta", runtime.get("restart_delta")],
                ["LPR evidence", runtime.get("lpr_evidence", {}).get("reason")],
            ],
        ),
        "",
        "## 2. Hardware và queue metrics",
        "",
        _md_table(
            ["Metric", "Value"],
            [
                ["Container RAM max (bytes)", resources.get("ram_max_bytes")],
                ["CPU max (%)", resources.get("cpu_percent_max")],
                ["GPU utilization max (%)", gpu.get("utilization_percent_max")],
                ["VRAM used max (MiB)", gpu.get("memory_used_mib_max")],
                ["VRAM total (MiB)", gpu.get("memory_total_mib")],
                ["/dev/shm max (%)", resources.get("shm_max_percent")],
                ["Queue depth samples", len(runtime.get("queue_metrics", {}).get("depth_samples", []))],
                ["Queue age (ms)", runtime.get("queue_metrics", {}).get("age_ms")],
                ["Queue age note", runtime.get("queue_metrics", {}).get("age_reason")],
                ["Sampler errors", "; ".join(runtime.get("hardware_sampler_errors", []))],
            ],
        ),
        "",
        "## 3. Stage trace và throughput",
        "",
        "### Detector / tracker / face / Event",
        "",
        _md_table(
            ["Stage", "Count", "Calls/s"],
            [
                [stage, count, trace.get("stage_calls_per_second", {}).get(stage)]
                for stage, count in sorted(trace.get("stage_counts", {}).items())
            ],
        ),
        "",
        "### LPR eligibility / plate detector / OCR",
        "",
        _md_table(
            ["Stage", "Count", "Calls/s"],
            [
                [stage, count, lpr_trace.get("stage_calls_per_second", {}).get(stage)]
                for stage, count in sorted(lpr_trace.get("stage_counts", {}).items())
            ],
        ),
        "",
        "## 4. LPR từng physical passage / round",
        "",
        _md_table(
            ["Passage", "Round", "Detected", "Plate", "Exact", "Assigned track"],
            [
                [
                    row.get("passage_id"),
                    row.get("round_id"),
                    row.get("detected"),
                    row.get("plate") or row.get("observed_plate"),
                    row.get("exact"),
                    row.get("track_id"),
                ]
                for row in lpr.get("passages", [])
            ],
        ),
        "",
        "## 5. Face từng physical passage / round",
        "",
        _md_table(
            ["Passage", "Round", "Detected", "Identity", "Correct", "Latency ms"],
            [
                [
                    row.get("passage_id"),
                    row.get("round_id"),
                    row.get("detected"),
                    row.get("identity") or row.get("observed_identity"),
                    row.get("correct"),
                    row.get("passage_to_confirmed_ms"),
                ]
                for row in face.get("passages", [])
            ],
        ),
        "",
        "## 6. Source PTS gap",
        "",
        _md_table(
            ["Camera", "Records with PTS", "Max gap (s)", "Mean gap (s)", "Missing PTS"],
            [
                [camera, values.get("records_with_source_pts"), values.get("max_gap_seconds"), values.get("mean_gap_seconds"), values.get("missing_source_pts")]
                for camera, values in sorted(runtime.get("source_pts", {}).items())
            ],
        ),
        "",
        "## 7. Evidence images",
        "",
        "Ảnh được sinh từ runtime evidence và mismatch evidence; mở trực tiếp từ các link dưới đây.",
        "",
    ]
    image_paths = sorted(
        path for path in output.rglob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    image_rows = []
    for path in image_paths:
        relative = path.relative_to(output).as_posix()
        image_rows.append([f"![{path.name}]({relative})", f"`{relative}`"])
    lines.extend([_md_table(["Preview", "Artifact"], image_rows), "", "## 8. JSON/log artifacts", ""])
    artifact_rows = []
    for artifact in summary.get("report", {}).get("artifacts", []):
        name = artifact.get("path", "")
        artifact_rows.append([f"[{name}]({name})", artifact.get("bytes"), artifact.get("sha256")])
    lines.append(_md_table(["Artifact", "Bytes", "SHA-256"], artifact_rows))
    lines.extend(["", "## 9. Diagnostic notes", "", "- Đây là báo cáo quan sát; không có tiêu chí pass/fail."])
    if runtime.get("bad_log_lines"):
        lines.append(f"- Runtime log warnings: `{len(runtime['bad_log_lines'])}` dòng.")
    if summary.get("error"):
        lines.append(f"- Error: `{summary['error']}`")
    report_path = output / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def write_failure_only_report(output: Path, summary: dict[str, Any]) -> Path:
    """Write a compact report containing only failed lifecycle traces."""
    runtime = summary.get("runtime", {})
    failed_lpr = [
        row for row in summary.get("lpr", {}).get("passages", [])
        if not row.get("detected") or row.get("exact") is False
    ]
    failed_face = [
        row for row in summary.get("face", {}).get("passages", [])
        if not row.get("correct")
    ]
    lpr_passage_labels = {
        str(row.get("passage_id")): f"lpr-{index:02d}"
        for index, row in enumerate(summary.get("lpr", {}).get("passages", []), 1)
    }

    def display_passage(kind: str, passage_id: str) -> str:
        return lpr_passage_labels.get(passage_id, passage_id) if kind == "LPR" else passage_id

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def add_records(kind: str, passage_id: str, records: list[dict[str, Any]]) -> None:
        for record in records:
            trace_id = str(
                record.get("trace_id")
                or record.get("track_id")
                or f"UNASSIGNED:{passage_id}"
            )
            groups.setdefault((kind, trace_id), []).append(record)

    for row in failed_lpr:
        for round_data in (row.get("round_trace") or {}).values():
            add_records("LPR", str(row.get("passage_id")), round_data.get("records", []))
    for row in failed_face:
        for round_data in row.get("rounds", []):
            trace = round_data.get("trace") or {}
            add_records("Face", str(row.get("passage_id")), trace.get("records", []))
    evidence_records: list[dict[str, Any]] = []
    evidence_path = output / "runtime-evidence.json"
    if evidence_path.is_file():
        evidence_records = json.loads(evidence_path.read_text(encoding="utf-8")).get(
            "records", []
        )
        failed_ids = {str(row.get("passage_id")) for row in failed_lpr}
        for record in evidence_records:
            if str(record.get("passage_id")) in failed_ids:
                add_records("LPR", str(record.get("passage_id")), [record])

    stage_order = (
        "detector_hit", "track_seen", "lpr_eligible", "plate_detector_input",
        "plate_detector_result", "plate_crop", "ocr_plate_input",
        "ocr_result", "ocr_text_crop", "ocr_recognition_tensor",
        "event_published", "first_qualified_face", "candidate_submitted",
        "first_attempt", "confirmed_result",
    )
    resources = runtime.get("resources", {})
    gpu = resources.get("gpu", {})
    timing = summary.get("timing", {})
    measurement = summary.get("measurement", {})
    trace_metrics_data = runtime.get("trace_metrics", {})
    lpr_trace_metrics = runtime.get("lpr_evidence_trace_metrics", {})

    def failure_value(record: dict[str, Any] | None) -> str:
        if record is None:
            return "MISSING"
        reason = record.get("reason")
        accepted = record.get("accepted")
        if accepted is False or record.get("status") in {"failed", "rejected", "error"}:
            return f"FAIL:{reason or record.get('status') or 'result'}"
        if reason and reason not in {"accepted", "eligible", "plate_detected"}:
            return f"FAIL:{reason}"
        if record.get("plate"):
            return f"PLATE:{record['plate']}"
        if record.get("identity"):
            return f"ID:{record['identity']}"
        if record.get("score") is not None:
            return f"PASS:{float(record['score']):.2f}"
        return "PASS"

    def failure_table_rows(
        kind: str, passages: list[dict[str, Any]], required: list[str]
    ) -> list[list[Any]]:
        rows: list[list[Any]] = []
        for passage in passages:
            passage_id = str(passage.get("passage_id"))
            source_records: list[dict[str, Any]] = []
            if kind == "LPR":
                for round_data in (passage.get("round_trace") or {}).values():
                    source_records.extend(round_data.get("records", []))
                source_records.extend(
                    record for record in evidence_records
                    if str(record.get("passage_id")) == passage_id
                )
            else:
                for round_data in passage.get("rounds", []):
                    source_records.extend((round_data.get("trace") or {}).get("records", []))
            trace_ids = sorted({str(record.get("trace_id")) for record in source_records if record.get("trace_id")}) or [f"UNASSIGNED:{passage_id}"]
            for trace_id in trace_ids:
                trace_records = [record for record in source_records if str(record.get("trace_id")) == trace_id]
                by_stage: dict[str, dict[str, Any]] = {}
                for record in trace_records:
                    by_stage.setdefault(str(record.get("stage")), record)
                rows.append([
                    kind,
                    display_passage(kind, passage_id),
                    trace_id,
                    f"[replay](media/{kind.lower()}/replay/{kind.lower()}-replay.mp4)",
                    passage.get("mismatch_reason") or ("recognition_not_correct" if kind == "Face" else "not_detected_or_not_exact"),
                    *[failure_value(by_stage.get(stage)) for stage in required],
                ])
        return rows

    lines = [
        "# Runtime Test Report",
        "",
        f"- **Run:** `{output.name}`",
        f"- **Status:** `{summary.get('report', {}).get('status', '-')}`",
        f"- **Measurement valid:** `{summary.get('measurement', {}).get('measurement_valid', '-')}`",
        "",
        "Metrics và kết quả tổng hợp giữ đầy đủ. Chỉ phần failure trace bên dưới loại trace/pass và ảnh pass.",
        "",
        "## Runtime metrics",
        "",
        _md_table(
            ["Metric", "Value"],
            [
                ["Total seconds", timing.get("total_seconds")],
                ["Replay seconds", timing.get("replay_seconds")],
                ["Restore seconds", timing.get("restore_seconds")],
                ["Rounds complete", runtime.get("rounds_complete")],
                ["Source PTS complete", measurement.get("source_pts_complete")],
                ["Measurement valid", measurement.get("measurement_valid")],
                ["Pending", runtime.get("pending")],
                ["Restart delta", runtime.get("restart_delta")],
                ["LPR evidence", runtime.get("lpr_evidence", {}).get("reason")],
            ],
        ),
        "",
        "## Hardware / queue performance",
        "",
        _md_table(
            ["Metric", "Value"],
            [
                ["Container RAM max (bytes)", resources.get("ram_max_bytes")],
                ["CPU max (%)", resources.get("cpu_percent_max")],
                ["GPU utilization max (%)", gpu.get("utilization_percent_max")],
                ["VRAM used max (MiB)", gpu.get("memory_used_mib_max")],
                ["VRAM total (MiB)", gpu.get("memory_total_mib")],
                ["/dev/shm max (%)", resources.get("shm_max_percent")],
                ["Skipped FPS max", resources.get("skipped_fps_max")],
                ["Queue depth samples", len(runtime.get("queue_metrics", {}).get("depth_samples", []))],
                ["Queue age (ms)", runtime.get("queue_metrics", {}).get("age_ms")],
                ["Queue age note", runtime.get("queue_metrics", {}).get("age_reason")],
                ["Sampler errors", "; ".join(runtime.get("hardware_sampler_errors", []))],
            ],
        ),
        "",
        "## Result summary",
        "",
        _md_table(
            ["Pipeline", "Recall", "Precision", "Accuracy", "Exact match", "Observed / expected"],
            [
                ["LPR", summary.get("lpr", {}).get("passage_recall"), summary.get("lpr", {}).get("passage_precision"), summary.get("lpr", {}).get("accuracy"), summary.get("lpr", {}).get("exact_match"), f"{runtime.get('lpr_evidence', {}).get('observed_passages', '-')} / {runtime.get('lpr_evidence', {}).get('expected_passages', '-')}"],
                ["Face", summary.get("face", {}).get("recall"), summary.get("face", {}).get("precision"), summary.get("face", {}).get("accuracy"), "-", "-"],
            ],
        ),
        "",
        "## Stage throughput",
        "",
        _md_table(
            ["Pipeline", "Stage", "Count", "Calls/s"],
            [
                ["Runtime", stage, count, trace_metrics_data.get("stage_calls_per_second", {}).get(stage)]
                for stage, count in sorted(trace_metrics_data.get("stage_counts", {}).items())
            ]
            + [
                ["LPR evidence", stage, count, lpr_trace_metrics.get("stage_calls_per_second", {}).get(stage)]
                for stage, count in sorted(lpr_trace_metrics.get("stage_counts", {}).items())
            ],
        ),
        "",
        "## Source PTS",
        "",
        _md_table(
            ["Camera", "Records", "Max gap (s)", "Mean gap (s)", "Missing"],
            [
                [camera, value.get("records_with_source_pts"), value.get("max_gap_seconds"), value.get("mean_gap_seconds"), value.get("missing_source_pts")]
                for camera, value in sorted(runtime.get("source_pts", {}).items())
            ],
        ),
        "",
        "## Trace summary",
        "",
        "### LPR",
        "",
        _md_table(
            ["Pipeline", "Passage", "trace_id", "Replay", "Reason", "detector_hit", "track_seen", "lpr_eligible", "plate_detector_result", "ocr_result", "event_published"],
            failure_table_rows(
                "LPR",
                failed_lpr,
                ["detector_hit", "track_seen", "lpr_eligible", "plate_detector_result", "ocr_result", "event_published"],
            ),
        ),
        "",
        "### Face",
        "",
        _md_table(
            ["Pipeline", "Passage", "trace_id", "Replay", "Reason", "detector_hit", "track_seen", "first_qualified_face", "candidate_submitted", "first_attempt", "confirmed_result"],
            failure_table_rows(
                "Face",
                failed_face,
                ["detector_hit", "track_seen", "first_qualified_face", "candidate_submitted", "first_attempt", "confirmed_result"],
            ),
        ),
    ]
    if not groups:
        lines.extend(["", "Không có failure trace."])
    else:
        lines.extend(["", "## Lifecycle traces", ""])
        evidence_root = output / "media"

        def stage_image(record: dict[str, Any], stage: str) -> Path | None:
            artifact_path = record.get("artifact_path")
            if artifact_path:
                candidate = output / "media" / str(artifact_path)
                if candidate.is_file():
                    return candidate
            track_id = str(record.get("track_id") or "")
            if not track_id or not evidence_root.is_dir():
                return None
            pipeline_root = evidence_root / ("face" if record.get("pipeline") == "face" or record.get("task") == "face" else "lpr")
            candidates = sorted(
                path
                for path in pipeline_root.rglob("*")
                if path.is_file()
                and stage in path.name
                and (track_id in path.parent.name or str(record.get("trace_id") or "") in str(path))
            )
            return candidates[0] if candidates else None

        def stage_result(record: dict[str, Any] | None) -> str:
            if record is None:
                return "MISSING"
            stage = str(record.get("stage"))
            field_groups = {
                "detector_hit": (
                    "label", "score", "object_box", "detection_region", "source_pts"
                ),
                "track_seen": (
                    "track_id", "generation", "object_box", "source_pts"
                ),
                "lpr_eligible": (
                    "accepted", "reason", "position_changes", "stationary",
                    "motionless_count", "eligibility_retry"
                ),
                "plate_detector_input": (
                    "artifact_path", "scale", "detection_threshold", "object_box"
                ),
                "plate_detector_result": (
                    "accepted", "reason", "score", "box", "plate_box"
                ),
                "plate_crop": (
                    "artifact_path", "artifact_bytes", "image_shape", "plate_box"
                ),
                "ocr_plate_input": (
                    "artifact_path", "artifact_bytes", "image_shape", "ocr_variant"
                ),
                "ocr_result": (
                    "accepted", "reason", "plate", "normalized_plate", "score",
                    "mean_character_score", "character_scores", "ocr_path"
                ),
                "ocr_text_crop": (
                    "artifact_path", "artifact_bytes", "image_shape", "text_box"
                ),
                "ocr_recognition_tensor": (
                    "artifact_path", "artifact_bytes", "image_shape", "ocr_variant",
                    "score"
                ),
                "event_published": (
                    "published", "accepted", "reason", "event_id", "plate",
                    "score", "identity"
                ),
                "first_qualified_face": (
                    "admitted", "reason", "person_box", "face_box", "quality_score",
                    "quality_components", "source_pts"
                ),
                "candidate_submitted": (
                    "candidate_id", "frame_id", "evidence_id", "identity",
                    "quality_score", "admitted", "source_pts"
                ),
                "first_attempt": (
                    "attempt", "task", "top1", "top2", "margin", "latency_ms",
                    "status", "reason"
                ),
                "confirmed_result": (
                    "identity", "confidence", "margin", "status", "reason",
                    "event_id", "source_pts"
                ),
            }
            fields = field_groups.get(
                stage,
                (
                    "reason", "decision", "accepted", "score", "plate", "identity",
                    "track_id", "candidate_id", "evidence_id", "event_id",
                    "artifact_path", "source_pts"
                ),
            )
            values = {key: record.get(key) for key in fields}
            values = {key: value for key, value in values.items() if value is not None}
            return json.dumps(values, ensure_ascii=False, separators=(",", ":")) or "{}"
        for (kind, trace_id), records in sorted(groups.items()):
            required_stages = (
                ["detector_hit", "track_seen", "lpr_eligible", "plate_detector_input",
                 "plate_detector_result", "plate_crop", "ocr_plate_input",
                 "ocr_result", "ocr_text_crop", "ocr_recognition_tensor", "event_published"]
                if kind == "LPR"
                else ["detector_hit", "track_seen", "first_qualified_face",
                      "candidate_submitted", "first_attempt", "confirmed_result"]
            )
            first_by_stage: dict[str, dict[str, Any]] = {}
            for record in sorted(
                records,
                key=lambda item: float(
                    item.get("source_pts", item.get("frame_time", 0)) or 0
                ),
            ):
                stage = str(record.get("stage"))
                if stage in required_stages and stage not in first_by_stage:
                    first_by_stage[stage] = record
            lifecycle_rows = []
            for stage in required_stages:
                record = first_by_stage.get(stage)
                image_path = stage_image(record or {}, stage) if record else None
                image_link = (
                    f"![evidence]({image_path.relative_to(output).as_posix()})"
                    if image_path
                    else "-"
                )
                lifecycle_rows.append(
                    [
                        stage,
                        (record or {}).get("source_pts", (record or {}).get("frame_time")),
                        (record or {}).get("trace_time"),
                        "MISSING" if record is None else "observed",
                        stage_result(record),
                        image_link,
                    ]
                )
            lines.extend([
                f"### `{kind}` track_id `{trace_id}`",
                "",
                _md_table(
                    ["Stage", "Source PTS", "Runtime time", "Status", "Result", "Image"],
                    lifecycle_rows,
                ),
                "",
            ])
    diagnostic_failures = []
    if not runtime.get("lpr_evidence", {}).get("valid", True):
        evidence = runtime.get("lpr_evidence", {})
        diagnostic_failures.append([
            "lpr_evidence",
            (
                f"{evidence.get('reason')}; expected={evidence.get('expected_passages')}; "
                f"observed={evidence.get('observed_passages')}; "
                f"missing={', '.join(evidence.get('missing_passages', [])) or '-'}"
            ),
        ])
    if runtime.get("correlation_mismatches"):
        diagnostic_failures.append(["correlation", len(runtime["correlation_mismatches"])])
    if runtime.get("hardware_sampler_errors"):
        diagnostic_failures.append(["hardware_sampler", "; ".join(runtime["hardware_sampler_errors"])])
    if runtime.get("bad_log_lines"):
        diagnostic_failures.append(["runtime_logs", len(runtime["bad_log_lines"])])
    if not summary.get("measurement", {}).get("source_pts_complete", True):
        diagnostic_failures.append(["source_pts", "missing source PTS"])
    if diagnostic_failures:
        lines.extend(["## Diagnostic failures", "", _md_table(["Area", "Detail"], diagnostic_failures), ""])
    report_path = output / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def cleanup_runtime_output(output: Path) -> None:
    """Keep only reproducibility, report, logs and runtime evidence artifacts."""
    output = output.resolve()
    removable_files = ("runtime-trace.jsonl",)
    for name in removable_files:
        path = output / name
        if path.is_file() and path.resolve().is_relative_to(output):
            path.unlink()
    removable_dirs = (
        output / "media" / "clips",
        output / "media" / "recordings",
        output / "media" / ".face-commits",
        output / "media" / "exports",
    )
    for path in removable_dirs:
        if path.is_dir() and path.resolve().is_relative_to(output):
            shutil.rmtree(path)
    media_root = output / "media"
    replay_root = media_root
    if media_root.is_dir():
        for prefix in ("face", "lpr"):
            source = output / f"{prefix}-replay.mp4"
            destination = media_root / prefix / "replay"
            if source.is_file():
                destination.mkdir(parents=True, exist_ok=True)
                target = destination / source.name
                if target.exists():
                    target.unlink()
                shutil.move(str(source), str(target))
        legacy_replay_root = media_root / "replays"
        if legacy_replay_root.is_dir():
            for prefix in ("face", "lpr"):
                legacy_dir = legacy_replay_root / prefix
                destination = replay_root / prefix / "replay"
                if legacy_dir.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    for path in legacy_dir.iterdir():
                        target = destination / path.name
                        if target.exists():
                            target.unlink()
                        shutil.move(str(path), str(target))
                    legacy_dir.rmdir()
            if legacy_replay_root.exists() and not any(legacy_replay_root.iterdir()):
                legacy_replay_root.rmdir()
        for prefix in ("face", "lpr"):
            destination = replay_root / prefix / "replay"
            destination.mkdir(parents=True, exist_ok=True)
            for path in media_root.iterdir():
                if not path.is_file() or not path.name.startswith(f"{prefix}-"):
                    continue
                target = destination / path.name
                if target.exists():
                    target.unlink()
                shutil.move(str(path), str(target))

    passage_evidence_root = media_root / "passage-evidence"
    evidence_index = passage_evidence_root / "evidence.jsonl"
    records = []
    if evidence_index.is_file():
        records = [
            json.loads(line)
            for line in evidence_index.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if records:
        by_pipeline: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for record in records:
            by_pipeline[str(record.get("pipeline") or "lpr")].append(record)
        for pipeline, pipeline_records in by_pipeline.items():
            source_root = passage_evidence_root / pipeline
            destination_root = media_root / pipeline
            destination_root.mkdir(parents=True, exist_ok=True)
            # New producer layout is already pipeline/trace_id/evidence_id.
            # Move the complete trace tree without reconstructing lineage here.
            if source_root.is_dir():
                for trace_dir in list(source_root.iterdir()):
                    if not trace_dir.is_dir():
                        continue
                    destination = destination_root / trace_dir.name
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.move(str(trace_dir), str(destination))
            (destination_root / "evidence.jsonl").write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False) for record in pipeline_records
                )
                + "\n",
                encoding="utf-8",
            )
        evidence_index.unlink(missing_ok=True)
    if passage_evidence_root.exists():
        for child in list(passage_evidence_root.iterdir()):
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()
        if not any(passage_evidence_root.iterdir()):
            passage_evidence_root.rmdir()


def main() -> int:
    argparse.ArgumentParser(
        description="Run one Platform runtime evidence replay and create a timestamped report."
    ).parse_args()
    config = Path("deploy/config.yaml")
    manifest_path = Path("tools/fixtures/platform_passage_ground_truth.yaml")
    output_root = Path(".tmp/platform-runtime")
    run_id = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    output = (output_root / run_id).resolve()

    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": 2,
        "profile": "platform-runtime-evidence",
        "accepted": None,
        "acceptance": {
            "mode": "evidence_only",
            "status": "pending",
            "criteria": [],
        },
        "gates": {},
        "timing": {},
    }
    previous_trace_path = os.environ.get("PASSAGE_TRACE_PATH")
    previous_evidence_dir = os.environ.get("PASSAGE_EVIDENCE_DIR")
    previous_capture_cutoff = os.environ.get("PASSAGE_CAPTURE_CUTOFF_PATH")
    previous_evidence_bytes = os.environ.get("PASSAGE_EVIDENCE_MAX_BYTES")
    previous_evidence_records = os.environ.get("PASSAGE_EVIDENCE_MAX_RECORDS")
    previous_ready_seconds = os.environ.get("CAMERA_READY_STABLE_SECONDS")
    previous_skip_ready = os.environ.get("CAMERA_SKIP_READY_WAIT")
    previous_source_overlay = os.environ.get("CAMERA_SOURCE_OVERLAY")
    previous_anchor_black_seconds = os.environ.get("PASSAGE_ANCHOR_MIN_BLACK_SECONDS")
    runtime_started = False
    replays_paused = False
    sampler: ResourceSampler | None = None
    isolated_start_wall: float | None = None
    exit_code = 1
    try:
        step_started = time.monotonic()
        manifest = load_manifest(manifest_path, Path.cwd())
        contract = fixture_contract(manifest)
        prepare_args = [
            "--config",
            str(config),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output),
        ]
        base_config = yaml.safe_load(config.read_text(encoding="utf-8"))
        model_path = Path.cwd() / str(base_config["runtime"]["model_path"])
        cached_path = output / "fixture.json"
        cached = (
            json.loads(cached_path.read_text(encoding="utf-8"))
            if cached_path.is_file()
            else {}
        )
        cache_valid = (
            cached.get("builder_version") == 8
            and cached.get("manifest_sha256") == sha256(manifest_path)
            and cached.get("base_config_sha256") == sha256(config)
            and cached.get("model_sha256") == sha256(model_path)
            and (output / "face-replay.mp4").is_file()
            and (output / "lpr-replay.mp4").is_file()
            and Path(cached.get("enrollment_image", "")).is_file()
        )
        if not cache_valid:
            subprocess.run(
                [
                    sys.executable,
                    "tools/fixtures/prepare_passage_fixture.py",
                    *prepare_args,
                ],
                check=True,
                timeout=30,
            )
        fixture = json.loads((output / "fixture.json").read_text(encoding="utf-8"))
        isolated_config = Path(fixture["config"])
        value = yaml.safe_load(isolated_config.read_text(encoding="utf-8"))
        value["runtime"]["image"] = base_config["runtime"]["image"]
        isolated_config.write_text(
            yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        fixture["config_sha256"] = sha256(isolated_config)
        write_json(output / "fixture.json", fixture)
        summary["fixture"] = fixture
        summary["fixture_contract"] = contract
        summary["timing"]["fixture_seconds"] = round(time.monotonic() - step_started, 3)
        # A prior run may still have this output's SQLite database bind-mounted.
        # Stop Frigate before resetting run-owned artifacts so SQLite can finish
        # WAL/checkpoint work and release its files cleanly.
        runtime_started = True
        docker_output("stop", "--time", "10", "frigate", timeout=15, check=False)
        artifact_dir = output / "media" / "clips" / "artifacts"
        if artifact_dir.is_dir() and artifact_dir.resolve().is_relative_to(output):
            shutil.rmtree(artifact_dir)
        runtime_evidence_dir = output / "media" / "passage-evidence"
        if (
            runtime_evidence_dir.is_dir()
            and runtime_evidence_dir.resolve().is_relative_to(output)
        ):
            shutil.rmtree(runtime_evidence_dir)
        database_dir = output / "media" / "passage"
        if database_dir.is_dir() and database_dir.resolve().is_relative_to(output):
            shutil.rmtree(database_dir)
        database_dir.mkdir(parents=True, exist_ok=True)

        isolated_start_wall = time.time()
        step_started = time.monotonic()
        os.environ["PASSAGE_TRACE_PATH"] = TRACE_CONTAINER_PATH
        os.environ["PASSAGE_EVIDENCE_DIR"] = EVIDENCE_CONTAINER_DIR
        os.environ["PASSAGE_CAPTURE_CUTOFF_PATH"] = CAPTURE_CUTOFF_CONTAINER_PATH
        os.environ["PASSAGE_EVIDENCE_MAX_BYTES"] = str(128 * 1024**2)
        os.environ["PASSAGE_EVIDENCE_MAX_RECORDS"] = "4096"
        os.environ["CAMERA_READY_STABLE_SECONDS"] = "1"
        os.environ["CAMERA_SKIP_READY_WAIT"] = "1"
        # The replay publisher can be online before Frigate subscribes, so the
        # initial black lead may be missed.  The measured inter-loop black
        # boundary remains distinct from internal black gaps at this bound.
        os.environ["PASSAGE_ANCHOR_MIN_BLACK_SECONDS"] = "0.4"
        try:
            run_deploy("acceptance-start", Path(fixture["config"]), timeout=30)
        finally:
            if previous_source_overlay is None:
                os.environ.pop("CAMERA_SOURCE_OVERLAY", None)
            else:
                os.environ["CAMERA_SOURCE_OVERLAY"] = previous_source_overlay
        wait_acceptance_ready(str(value["runtime"]["image"]), timeout=40)
        summary["timing"]["isolated_start_seconds"] = round(time.monotonic() - step_started, 3)
        observation_wall = time.time()
        subprocess.run(
            ["docker", "exec", "frigate", "rm", "-f", TRACE_CONTAINER_PATH],
            check=False,
            timeout=10,
        )
        initial_restarts = restart_counts()
        sampler = ResourceSampler()
        sampler.start()

        replays = {"face": output / "face-replay.mp4", "lpr": output / "lpr-replay.mp4"}
        step_started = time.monotonic()
        # Preserve the final passage and following black boundary for the
        # single replay round.
        anchors, anchor_details = observe_round_anchors(replays, started + 103)
        capture_cutoff = time.time()
        summary["capture_cutoff_epoch"] = capture_cutoff
        docker_output(
            "exec",
            "frigate",
            "sh",
            "-c",
            f"printf '%s\\n' '{capture_cutoff:.9f}' > {CAPTURE_CUTOFF_CONTAINER_PATH}",
            timeout=5,
        )
        recognition_idle = wait_recognition_idle()
        # Let tracker generations and quality leases expire while the replay
        # still supplies frames.  Pausing first leaves the last active track
        # pinned forever because no subsequent disappearance frame arrives.
        docker_output(
            "pause",
            "camera-replay-face-camera",
            "camera-replay-car-camera",
            timeout=5,
        )
        replays_paused = True
        summary["timing"]["replay_seconds"] = round(time.monotonic() - step_started, 3)
        sampler.stop()
        resource_memory = list(sampler.memory_bytes)
        hardware_cpu_samples = list(sampler.cpu_percent)
        hardware_gpu_samples = list(sampler.gpu_samples)
        hardware_sampler_errors = list(sampler.errors)
        resource_shm = list(sampler.shm_percent)
        skipped_fps = {
            camera: max(values, default=0.0)
            for camera, values in sampler.skipped_fps.items()
        }
        evidence_bytes = {
            camera: max(values, default=0)
            for camera, values in sampler.evidence_bytes.items()
        }
        evidence_pinned_final = (
            sampler.evidence_pinned[-1] if sampler.evidence_pinned else None
        )
        evidence_pinned_min = min(sampler.evidence_pinned, default=None)
        lifecycle_stats = (
            sampler.recognition_lifecycle[-1] if sampler.recognition_lifecycle else {}
        )
        final_restarts = restart_counts()

        evidence_manifest = (
            output / "media" / "passage-evidence" / "evidence.jsonl"
        )
        if not wait_file_quiescent(evidence_manifest):
            raise RuntimeError(
                "Runtime LPR evidence writer did not become quiescent after replay"
            )

        trace_path = output / "runtime-trace.jsonl"
        trace_path.unlink(missing_ok=True)
        subprocess.run(
            ["docker", "cp", f"frigate:{TRACE_CONTAINER_PATH}", str(trace_path)],
            check=False,
            timeout=10,
        )
        if not trace_path.is_file():
            raise RuntimeError("Passage runtime trace is missing")
        records = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        face_passages = merge_passages(manifest, fixture, "face")
        lpr_passages = merge_passages(manifest, fixture, "lpr")
        durations = {
            CAMERAS[kind]: replay_duration(path) for kind, path in replays.items()
        }
        passages_by_camera = {
            CAMERAS["face"]: face_passages,
            CAMERAS["lpr"]: lpr_passages,
        }
        # Runtime lineage is producer-owned.  Keep this record set immutable for
        # runtime metrics; fixture association is a separate comparison view.
        runtime_records = records
        comparison_records = assign_records(
            [dict(record) for record in runtime_records],
            anchors,
            durations,
            passages_by_camera,
        )
        replay_seconds = float(summary["timing"].get("replay_seconds", 0.0))
        observed_trace_metrics = trace_metrics(runtime_records, replay_seconds)
        observed_source_pts = source_pts_metrics(runtime_records)
        evidence_records, runtime_lpr_evidence = validate_runtime_lpr_evidence(
            output / "media" / "passage-evidence",
            anchors,
            durations,
            passages_by_camera,
        )
        observed_lpr_trace_metrics = trace_metrics(evidence_records, replay_seconds)
        write_json(
            output / "runtime-evidence.json",
            {"summary": runtime_lpr_evidence, "records": evidence_records},
        )
        recognition = recognition_lifecycle_summary(runtime_records, lifecycle_stats)
        write_json(
            output / "runtime-trace.json", {"anchors": anchors, "records": runtime_records}
        )
        write_json(
            output / "fixture-comparison.json",
            {"anchors": anchors, "records": comparison_records},
        )

        (
            face,
            face_false,
            passage_latency,
            eligible_latency,
            first_attempt,
            embeddings,
        ) = face_results(comparison_records, face_passages, anchors[CAMERAS["face"]])
        lpr, lpr_false = lpr_results(comparison_records, lpr_passages)
        correlation = correlation_mismatches(comparison_records)
        runtime_logs = docker_output(
            "logs",
            "frigate",
            "--since",
            str(int(observation_wall)),
            timeout=15,
            check=False,
        )
        model_logs = docker_output(
            "logs",
            "frigate",
            "--since",
            str(int(isolated_start_wall)),
            timeout=15,
            check=False,
        )
        pending = parse_pending(runtime_logs)
        pending_source = "face_pipeline_log"
        if (
            int(lifecycle_stats.get("in_flight", 0)) == 0
            and evidence_pinned_final == 0
        ):
            # The periodic face log can be older than the final sampler point.
            # Lifecycle plus evidence ownership is the authoritative live state.
            pending = 0
            pending_source = "lifecycle_and_evidence"
        bad_log_lines = [
            line
            for line in runtime_logs.splitlines()
            if re.search(
                r"reconnect|stall|no frames|ffmpeg.*(?:exited|error)", line, re.I
            )
        ]
        api_consistent, api_mismatches, uncommitted_updates = api_sqlite_consistency(
            records, str(value["database"]["path"])
        )
        restart_delta = max(
            (
                final_restarts.get(name, 0) - initial_restarts.get(name, 0)
                for name in set(initial_restarts) | set(final_restarts)
            ),
            default=0,
        )
        detector_count = len(
            yaml.safe_load(Path(fixture["config"]).read_text(encoding="utf-8")).get(
                "detectors", {}
            )
        )
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
            "cpu_percent_max": max(hardware_cpu_samples, default=None),
            "gpu": {
                "utilization_percent_max": max(
                    (sample["utilization_percent"] for sample in hardware_gpu_samples),
                    default=None,
                ),
                "memory_used_mib_max": max(
                    (sample["memory_used_mib"] for sample in hardware_gpu_samples),
                    default=None,
                ),
                "memory_total_mib": max(
                    (sample["memory_total_mib"] for sample in hardware_gpu_samples),
                    default=None,
                ),
            },
            "shm_max_percent": max(resource_shm, default=None),
            "skipped_fps_max": skipped_fps,
            "evidence_bytes_max": evidence_bytes,
            "evidence_pinned_final": evidence_pinned_final,
            "evidence_pinned_min": evidence_pinned_min,
        }
        if resources["ram_max_bytes"] is None:
            usage = docker_output(
                "stats",
                "--no-stream",
                "--format",
                "{{.MemUsage}}",
                "frigate",
                timeout=5,
            )
            resources["ram_max_bytes"] = parse_bytes(usage.split("/")[0])
            line = docker_output(
                "exec", "frigate", "sh", "-c", "df -P /dev/shm | tail -1", timeout=5
            )
            resources["shm_max_percent"] = float(
                next(token for token in line.split() if token.endswith("%")).rstrip("%")
            )
        runtime = {
            "anchors": anchor_details,
            "rounds_complete": all(len(value) == ROUNDS for value in anchors.values()),
            "pending": pending,
            "pending_source": pending_source,
            "restart_delta": restart_delta,
            "bad_log_lines": bad_log_lines,
            "correlation_mismatches": correlation,
            "api_sqlite_mismatches": api_mismatches,
            "uncommitted_updates": uncommitted_updates,
            "model_loads": {
                "detector_expected": detector_count,
                "detector_actual": model_loads,
                "face_expected": 1,
                "face_actual": face_model_loads,
            },
            "resources": resources,
            "hardware_samples": {
                "cpu_percent": hardware_cpu_samples,
                "gpu": hardware_gpu_samples,
                "ram_bytes": resource_memory,
                "shm_percent": resource_shm,
            },
            "hardware_sampler_errors": hardware_sampler_errors,
            "trace_metrics": observed_trace_metrics,
            "lpr_evidence_trace_metrics": observed_lpr_trace_metrics,
            "source_pts": observed_source_pts,
            "queue_metrics": {
                "depth_samples": sampler.recognition_lifecycle,
                "age_ms": None,
                "age_reason": "runtime stats exposes queue depth but not per-item enqueue age",
            },
            "lpr_evidence": runtime_lpr_evidence,
            "recognition_lifecycle": recognition,
            "recognition_idle_after_replay": recognition_idle,
        }
        summary.update(
            {
                "face": face,
                "lpr": lpr,
                "latency": latency,
                "runtime": runtime,
                "source_hash": {
                    "manifest": sha256(manifest_path),
                    "config": sha256(Path(fixture["config"])),
                    "model": fixture.get("model_sha256"),
                },
            }
        )
        write_json(output / "face.json", {**face, "latency": latency["face"]})
        write_json(output / "lpr.json", lpr)

        # Runtime stage evidence under media/passage-evidence is authoritative.
        # Do not create a duplicate ground-truth mismatch folder.
        evidence_ok = True

        gates = {
            "fixture_contract": contract["valid"],
            "anchor_complete": runtime["rounds_complete"],
            "lpr_recall": lpr["passage_recall"] == 1.0,
            "lpr_readable_denominator": lpr["readable_denominator"] >= 3,
            "lpr_exact_match_reported": lpr["exact_match"] is not None,
            "lpr_accuracy": lpr["accuracy"] is not None
            and lpr["accuracy"] >= 2 / 3,
            "lpr_recognition_precision": lpr["precision"] == 1.0,
            "lpr_recognition_recall": lpr["recall"] >= 2 / 3,
            "lpr_passage_precision": lpr["passage_precision"] == 1.0,
            "face_detection_recall": face["detection_recall"] >= MIN_PASSAGE_RATE,
            "face_accuracy": face["accuracy"] >= 0.8,
            "face_precision": face["precision"] == 1.0,
            "face_recall": face["recall"] >= 0.8,
            "face_passage_latency": latency["face"]["passage_to_confirmed_ms_p95"]
            is not None
            and latency["face"]["passage_to_confirmed_ms_p95"] <= 3000,
            "face_eligible_latency": latency["face"]["eligible_to_confirmed_ms_p95"]
            is not None
            and latency["face"]["eligible_to_confirmed_ms_p95"] <= 1500,
            "face_first_attempt": latency["face"]["first_attempt_ms_p95"] is not None
            and latency["face"]["first_attempt_ms_p95"] <= 750,
            "face_embedding": latency["face"]["embedding_ms_p95"] is not None
            and latency["face"]["embedding_ms_p95"] <= 200,
            "pending_zero": pending == 0,
            "correlation": not correlation,
            "api_sqlite_consistency": api_consistent,
            "evidence": evidence_ok,
            "lpr_runtime_evidence": runtime_lpr_evidence["valid"],
            "restarts": restart_delta == 0,
            "no_reconnect_or_stall": not bad_log_lines,
            "ram": resources["ram_max_bytes"] is not None
            and resources["ram_max_bytes"] <= 7 * 1024**3,
            "shm": resources["shm_max_percent"] is not None
            and resources["shm_max_percent"] < 70,
            "model_load_once_per_instance": model_loads == detector_count
            and face_model_loads == 1,
            "evidence_bytes_per_camera": all(
                value <= 32 * 1024**2 for value in evidence_bytes.values()
            ),
            "recognition_attempt_budget": recognition["max_attempts_per_track"] <= 3,
            "recognition_duplicate_inference": not recognition["duplicate_inference"],
            "recognition_stale_results": lifecycle_stats.get("stale_results", 0) == 0,
            "recognition_early_stop_zero": lifecycle_stats.get("early_stop", 0) == 0,
            "recognition_in_flight_zero": lifecycle_stats.get("in_flight", 0) == 0,
            "recognition_active_lifecycle_zero": lifecycle_stats.get(
                "active_lifecycles", lifecycle_stats.get("active_tracks", 0)
            )
            == 0,
            "recognition_selector_depth_zero": lifecycle_stats.get(
                "quality_top_k_depth", 0
            )
            == 0,
            "recognition_lpr_queue_zero": lifecycle_stats.get("lpr_queue_depth", 0)
            == 0,
            "evidence_pinned_zero": evidence_pinned_final == 0,
        }
        summary["gates"] = gates
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary.setdefault("gates", {})["error_free"] = False
    finally:
        try:
            capture_container_diagnostics(output, isolated_start_wall)
        except Exception as exc:
            summary.setdefault("diagnostic_errors", []).append(
                f"{type(exc).__name__}: {exc}"
            )
        if replays_paused:
            docker_output(
                "unpause",
                "camera-replay-face-camera",
                "camera-replay-car-camera",
                timeout=5,
                check=False,
            )
        if sampler is not None:
            sampler.stop()
        step_started = time.monotonic()
        restore_ok = not runtime_started
        if runtime_started:
            try:
                run_deploy("acceptance-restore", config, timeout=30)
                restore_ok = restore_mounts_verified(config)
            except Exception as exc:
                summary.setdefault("restore_errors", []).append(
                    f"{type(exc).__name__}: {exc}"
                )
        summary["timing"]["restore_seconds"] = round(time.monotonic() - step_started, 3)
        summary["timing"]["total_seconds"] = round(time.monotonic() - started, 3)
        summary.setdefault("gates", {})["runtime_restored"] = restore_ok
        summary["gates"]["under_runtime_budget"] = (
            summary["timing"]["total_seconds"] < ACCEPTANCE_RUNTIME_BUDGET_SECONDS
        )
        # The calculated gates remain diagnostic data and never decide the report.
        summary["diagnostic_gates"] = summary["gates"]
        summary["acceptance"] = {
            "mode": "evidence_only",
            "status": "not_scored",
            "criteria": [],
        }
        summary["accepted"] = None
        summary["measurement"] = {
            "source_time": "source_pts",
            "assignment": "one_to_one_physical_passage_round",
            "recognition_summary": "per_passage_round_lineage",
            "wall_clock_used_for_scoring": False,
            "source_pts_complete": all(
                item.get("missing_source_pts", 0) == 0
                for item in summary.get("runtime", {}).get("source_pts", {}).values()
            ),
            "measurement_valid": bool(
                summary.get("runtime", {}).get("rounds_complete")
                and not summary.get("runtime", {}).get("correlation_mismatches")
                and all(
                    item.get("missing_source_pts", 0) == 0
                    for item in summary.get("runtime", {})
                    .get("source_pts", {})
                    .values()
                )
                and summary.get("runtime", {})
                .get("lpr_evidence", {})
                .get("valid")
            ),
        }
        artifact_names = (
            "runtime-trace.json",
            "runtime-evidence.json",
            "face.json",
            "lpr.json",
            "container-inspect.json",
            "container.log",
        )
        artifacts_complete = all((output / name).is_file() for name in artifact_names)
        summary["report"] = {
            "mode": "evidence_only",
            "status": "complete"
            if "error" not in summary and artifacts_complete
            else "incomplete",
            "criteria": [],
            "artifacts": [
                {
                    "path": name,
                    "bytes": (output / name).stat().st_size,
                    "sha256": sha256(output / name),
                }
                for name in artifact_names
                if (output / name).is_file()
            ],
            "required_content": [
                "fixture_and_source_hashes",
                "all_physical_passages_and_rounds",
                "detector_track_event_lpr_face_funnel",
                "raw_ocr_and_quality_lineage",
                "runtime_cleanup_and_resource_diagnostics",
                "container_logs_and_artifact_hashes",
            ],
        }
        if previous_trace_path is None:
            os.environ.pop("PASSAGE_TRACE_PATH", None)
        else:
            os.environ["PASSAGE_TRACE_PATH"] = previous_trace_path
        if previous_capture_cutoff is None:
            os.environ.pop("PASSAGE_CAPTURE_CUTOFF_PATH", None)
        else:
            os.environ["PASSAGE_CAPTURE_CUTOFF_PATH"] = previous_capture_cutoff
        if previous_anchor_black_seconds is None:
            os.environ.pop("PASSAGE_ANCHOR_MIN_BLACK_SECONDS", None)
        else:
            os.environ["PASSAGE_ANCHOR_MIN_BLACK_SECONDS"] = previous_anchor_black_seconds
        for name, previous in (
            ("PASSAGE_EVIDENCE_DIR", previous_evidence_dir),
            ("PASSAGE_EVIDENCE_MAX_BYTES", previous_evidence_bytes),
            ("PASSAGE_EVIDENCE_MAX_RECORDS", previous_evidence_records),
        ):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        if previous_ready_seconds is None:
            os.environ.pop("CAMERA_READY_STABLE_SECONDS", None)
        else:
            os.environ["CAMERA_READY_STABLE_SECONDS"] = previous_ready_seconds
        if previous_skip_ready is None:
            os.environ.pop("CAMERA_SKIP_READY_WAIT", None)
        else:
            os.environ["CAMERA_SKIP_READY_WAIT"] = previous_skip_ready
        if previous_source_overlay is None:
            os.environ.pop("CAMERA_SOURCE_OVERLAY", None)
        else:
            os.environ["CAMERA_SOURCE_OVERLAY"] = previous_source_overlay
        write_json(output / "summary.json", summary)
        cleanup_runtime_output(output)
        report_path = write_failure_only_report(output, summary)
        summary["report"]["artifacts"].append(
            {
                "path": report_path.name,
                "bytes": report_path.stat().st_size,
                "sha256": sha256(report_path),
            }
        )
        write_json(output / "summary.json", summary)
        exit_code = 0 if summary["report"]["status"] == "complete" else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
