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
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import cv2
import numpy as np
import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.fixtures.prepare_passage_fixture import load_manifest
from tools.lib.passage_metrics import bbox_iou, normalize_plate, percentile

CAMERAS = {"face": "face_camera", "lpr": "car_camera"}
TRACE_CONTAINER_PATH = "/runtime-evidence/runtime-trace.jsonl"
EVIDENCE_CONTAINER_DIR = "/runtime-evidence"
CAPTURE_CUTOFF_CONTAINER_PATH = "/runtime-evidence/capture-cutoff"
CAPTURE_START_CONTAINER_PATH = "/runtime-evidence/capture-start"
SOURCE_START_CONTAINER_DIR = "/runtime-evidence/source-start"
LEAD_SECONDS = 0.0
ROUNDS = 1
MIN_PASSAGE_RATE = 0.8
MAX_SKIPPED_FPS_REGRESSION = 0.1
ACCEPTANCE_RUNTIME_BUDGET_SECONDS = 360.0
MASTER_RECOGNITION_COMMIT = "50a2b6729eb152d9512b100c78c55fa84dffa430"


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


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*.py")):
        digest.update(file.relative_to(path).as_posix().encode("utf-8"))
        digest.update(file.read_bytes())
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
    """Load and integrity-check runtime LPR evidence by producer trace."""
    manifest = evidence_dir / "lpr" / "evidence.jsonl"
    if not manifest.is_file():
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
                    "mean_character_score": ocr_result.get("mean_character_score"),
                    "character_scores": ocr_result.get("character_scores"),
                },
                "errors": invocation_errors,
            }
        )

    summary = {
        "valid": bool(grouped) and not errors,
        "reason": None if grouped and not errors else "incomplete_runtime_evidence",
        "invocations": len(grouped),
        "artifact_count": artifact_count,
        "artifact_bytes": artifact_bytes,
        "errors": errors,
        "invocation_summaries": invocation_summaries,
    }
    return records, summary


def validate_recognition_evidence(
    media_root: Path,
    runtime_records: list[dict[str, Any]],
    evidence_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Require source artifacts for attempts and typed reasons for skipped traces."""
    errors: list[str] = []
    interrupted_reasons = {
        "cancelled",
        "closed",
        "deadline_exceeded",
        "epoch_mismatch",
        "queue_full",
        "service_disconnected",
        "service_unavailable",
    }
    resolved_root = media_root.resolve()
    evidence_by_trace: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in evidence_records:
        trace_id = str(record.get("trace_id") or "")
        if trace_id:
            evidence_by_trace[trace_id].append(record)

    attempts = [
        record
        for record in runtime_records
        if record.get("pipeline") == "face"
        and record.get("stage") == "first_attempt"
    ]
    interrupted_traces = {
        str(record.get("trace_id"))
        for record in runtime_records
        if record.get("stage") == "recognition_failed"
        and record.get("trace_id")
        and str(record.get("reason") or "") in interrupted_reasons
    }
    for attempt in attempts:
        trace_id = str(attempt.get("trace_id") or "")
        frame_time = attempt.get("frame_time")
        matching = [
            record
            for record in evidence_by_trace.get(trace_id, ())
            if record.get("frame_time") == frame_time
        ]
        stages = {str(record.get("stage")) for record in matching}
        # A service/epoch interruption is a typed terminal outcome, not a
        # healthy recognition attempt.  The interruption can happen after
        # submission but before the worker emits its attempt artifacts, so
        # requiring the healthy artifact trio here would reject valid fault
        # injection evidence.  Any artifacts that do exist are still checked
        # by the generic integrity loop below.
        if trace_id in interrupted_traces:
            continue
        for stage in (
            "recognition_attempt",
            "recognition_attempt_bbox",
            "face_crop",
        ):
            if stage not in stages:
                errors.append(f"{trace_id}@{frame_time}:missing stage: {stage}")

        source = next(
            (record for record in matching if record.get("stage") == "recognition_attempt"),
            None,
        )
        crop = next(
            (record for record in matching if record.get("stage") == "face_crop"),
            None,
        )
        if source is None or crop is None:
            continue
        if source.get("bbox_format") != "xyxy_pixels":
            errors.append(f"{trace_id}@{frame_time}:invalid bbox format")
        if source.get("bbox_coordinate_space") != "recognition_attempt":
            errors.append(f"{trace_id}@{frame_time}:invalid bbox coordinate space")
        detail_box = source.get("effective_crop_box")
        source_shape = source.get("image_shape")
        crop_shape = crop.get("image_shape")
        if (
            not isinstance(detail_box, list)
            or len(detail_box) != 4
            or not isinstance(source_shape, list)
            or len(source_shape) < 2
            or not isinstance(crop_shape, list)
            or len(crop_shape) < 2
        ):
            errors.append(f"{trace_id}@{frame_time}:missing face bbox geometry")
            continue
        x1, y1, x2, y2 = (int(value) for value in detail_box)
        height, width = (int(source_shape[0]), int(source_shape[1]))
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            errors.append(f"{trace_id}@{frame_time}:face bbox outside source image")
        if [int(crop_shape[0]), int(crop_shape[1])] != [y2 - y1, x2 - x1]:
            errors.append(f"{trace_id}@{frame_time}:face crop does not match bbox")

        artifact_path = source.get("artifact_path")
        if artifact_path:
            sidecar = (media_root / str(artifact_path)).parent / "evidence.json"
            if not sidecar.is_file():
                errors.append(f"{trace_id}@{frame_time}:missing evidence.json")
            else:
                try:
                    sidecar_payload = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    errors.append(f"{trace_id}@{frame_time}:invalid evidence.json")
                else:
                    if (
                        sidecar_payload.get("bbox_format") != "xyxy_pixels"
                        or sidecar_payload.get("bbox_coordinate_space")
                        != "recognition_attempt"
                    ):
                        errors.append(
                            f"{trace_id}@{frame_time}:sidecar missing bbox contract"
                        )

    for trace_id, records in evidence_by_trace.items():
        for record in records:
            relative = record.get("artifact_path")
            if not relative:
                continue
            target = (media_root / str(relative)).resolve()
            if not target.is_relative_to(resolved_root) or not target.is_file():
                errors.append(f"{trace_id}:missing artifact: {relative}")
                continue
            if sha256(target) != record.get("artifact_sha256"):
                errors.append(f"{trace_id}:sha256 mismatch: {relative}")
            if target.stat().st_size != int(record.get("artifact_bytes", -1)):
                errors.append(f"{trace_id}:byte size mismatch: {relative}")

    tracked = {
        str(record.get("trace_id"))
        for record in runtime_records
        if record.get("stage") == "track_seen"
        and record.get("pipeline") in {"face", "lpr"}
        and record.get("trace_id")
    }
    explained = set(evidence_by_trace) | {
        str(record.get("trace_id"))
        for record in runtime_records
        if record.get("stage") in {
            "recognition_skipped",
            "recognition_failed",
            "ocr_result",
            "first_attempt",
        }
        and record.get("trace_id")
    }
    for trace_id in sorted(tracked - explained):
        errors.append(f"{trace_id}:missing source artifact or typed skip reason")

    return {
        "valid": bool(attempts) and not errors,
        "attempt_count": len(attempts),
        "face_artifact_count": sum(
            record.get("pipeline") == "face" and bool(record.get("artifact_path"))
            for record in evidence_records
        ),
        "errors": errors,
    }


# Backward-compatible import name for existing acceptance tests.
validate_external_recognition_evidence = validate_recognition_evidence


def run_deploy(
    command: str,
    config: Path | None = None,
    timeout: int = 45,
    fault_scenario: str | None = None,
) -> None:
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
    if fault_scenario is not None:
        args += ["-FaultScenario", fault_scenario]
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        detail = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
        raise TimeoutError(
            f"deploy/run.ps1 {command} timed out after {timeout} seconds"
            + (f":\n{detail}" if detail else "")
        ) from exc
    if completed.returncode != 0:
        detail = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part.strip()
        )
        raise RuntimeError(
            f"deploy/run.ps1 {command} failed ({completed.returncode}): {detail}"
        )


class LauncherFaultInjector:
    """Invoke runtime faults only through the shared launcher contract."""

    def __init__(self, scenario: str, output: Path, config: Path) -> None:
        self.scenario = scenario
        self.output = output
        self.config = config
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.error: str | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=65)
        if self.error:
            raise RuntimeError(self.error)

    def _run(self) -> None:
        if self.stop_event.wait(3):
            return
        try:
            run_deploy(
                "acceptance-fault",
                self.config,
                timeout=60,
                fault_scenario=self.scenario,
            )
            fault_path = Path(".tmp/runtime/fault.json")
            if not fault_path.is_file():
                raise RuntimeError("launcher fault artifact is missing")
            shutil.copy2(fault_path, self.output / "fault-record.json")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class DockerFaultInjector:
    """Execute one real Docker interruption and persist only observed facts."""

    SCENARIOS: ClassVar[set[str]] = {
        "service_restart", "stream_disconnect", "client_disconnect"
    }

    def __init__(self, scenario: str, output: Path, config: Path) -> None:
        if scenario not in self.SCENARIOS:
            raise ValueError(f"unsupported fault scenario: {scenario}")
        self.scenario = scenario
        self.output = output
        self.config = config
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.records: list[dict[str, Any]] = []

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)
        trace_path = self.output / "media" / "runtime-trace.jsonl"
        publication_count = 0
        if trace_path.is_file():
            publication_count = sum(
                1
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if '"stage": "event_published"' in line
            )
        for record in self.records:
            try:
                record["service_state_after"] = docker_output(
                    "inspect", "camera-recognition", "--format", "{{json .State}}", check=False
                )
                record["client_state_after"] = docker_output(
                    "inspect", "frigate", "--format", "{{json .State}}", check=False
                )
                record["service_logs_after"] = docker_output(
                    "logs", "camera-recognition", "--tail", "200", check=False
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics are best effort
                record["post_state_error"] = str(exc)
            record["event_publication_count"] = publication_count
            record["typed_lifecycle_outcomes"] = [
                status
                for status in (
                    "deadline_exceeded", "cancelled", "epoch_mismatch", "service_disconnected"
                )
                if status in str(record.get("service_logs_after", ""))
            ]
        write_json(self.output / "fault-record.json", {
            "schema_version": 1,
            "scenario": self.scenario,
            "records": self.records,
        })

    def _record(self, action: str, command: list[str], result: str = "") -> None:
        record = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "scenario": self.scenario,
            "action": action,
            "command": command,
            "result": result,
        }
        try:
            record["service_state_before"] = docker_output(
                "inspect", "camera-recognition", "--format", "{{json .State}}", check=False
            )
            record["client_state_before"] = docker_output(
                "inspect", "frigate", "--format", "{{json .State}}", check=False
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must survive Docker errors
            record["state_error"] = str(exc)
        self.records.append(record)

    def _run(self) -> None:
        # Wait until both finite replay publishers have had time to enqueue
        # pending observations; no synthetic trace/Event is ever written.
        if self.stop_event.wait(3.0):
            return
        if self.scenario == "service_restart":
            command = ["docker", "restart", "camera-recognition"]
            self._record("restart_service", command)
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.records[-1]["exit_code"] = completed.returncode
            self.records[-1]["restore"] = "docker restart completed"
        elif self.scenario == "stream_disconnect":
            network = docker_output("inspect", "frigate", "--format", "{{json .NetworkSettings.Networks}}", check=False)
            try:
                network_name = next(iter(json.loads(network)))
            except (StopIteration, TypeError, json.JSONDecodeError):
                self._record("disconnect_stream", [], "runtime_network_unavailable")
                return
            command = ["docker", "network", "disconnect", network_name, "frigate"]
            self._record("disconnect_stream", command, network_name)
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.records[-1]["exit_code"] = completed.returncode
            if not self.stop_event.wait(1.0):
                reconnect = ["docker", "network", "connect", network_name, "frigate"]
                reconnected = subprocess.run(reconnect, capture_output=True, text=True, check=False)
                self.records[-1]["reconnect"] = reconnect
                self.records[-1]["reconnect_exit_code"] = reconnected.returncode
        else:
            network = docker_output(
                "inspect", "camera-recognition", "--format",
                "{{json .NetworkSettings.Networks}}", check=False
            )
            try:
                network_name = next(iter(json.loads(network)))
            except (StopIteration, TypeError, json.JSONDecodeError):
                self._record("disconnect_client", [], "runtime_network_unavailable")
                return
            command = ["docker", "network", "disconnect", network_name, "camera-recognition"]
            self._record("disconnect_client", command, network_name)
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            self.records[-1]["exit_code"] = completed.returncode
            if not self.stop_event.wait(1.0):
                reconnect = ["docker", "network", "connect", network_name, "camera-recognition"]
                restored = subprocess.run(reconnect, capture_output=True, text=True, check=False)
                self.records[-1]["reconnect"] = reconnect
                self.records[-1]["reconnect_exit_code"] = restored.returncode
            self.records[-1]["restore"] = "docker network reconnect completed"


def create_service_tls(
    directory: Path, server_name: str, client_name: str = "frigate"
) -> None:
    """Create a run-scoped CA and server/client identities for mTLS."""
    openssl = shutil.which("openssl")
    directory.mkdir(parents=True, exist_ok=True)
    server_ext = directory / "server.ext"
    client_ext = directory / "client.ext"
    server_ext.write_text(
        f"subjectAltName=DNS:{server_name}\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    client_ext.write_text("extendedKeyUsage=clientAuth\n", encoding="utf-8")
    commands = (
        (
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", "ca.key", "-out", "ca.crt", "-days", "1",
            "-subj", f"/CN={server_name}-test-ca",
        ),
        (
            "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
            "-out", "server.csr", "-subj", f"/CN={server_name}",
        ),
        (
            "x509", "-req", "-in", "server.csr", "-CA", "ca.crt",
            "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.crt",
            "-days", "1", "-extfile", "server.ext",
        ),
        (
            "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "client.key",
            "-out", "client.csr", "-subj", f"/CN={client_name}",
        ),
        (
            "x509", "-req", "-in", "client.csr", "-CA", "ca.crt",
            "-CAkey", "ca.key", "-CAcreateserial", "-out", "client.crt",
            "-days", "1", "-extfile", "client.ext",
        ),
    )
    if openssl is None:
        script = "\n".join(
            "openssl " + subprocess.list2cmdline(list(command))
            for command in commands
        )
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{directory.resolve()}:/tls",
                "--workdir",
                "/tls",
                "--entrypoint",
                "/bin/sh",
                "camera-recognition:current",
                "-ec",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{server_name} TLS generation failed: {completed.stderr.strip()}"
            )
        return
    for command in commands:
        completed = subprocess.run(
            [openssl, *command],
            cwd=directory,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{server_name} TLS generation failed: {completed.stderr.strip()}"
            )


def create_recognition_tls(directory: Path) -> None:
    """Compatibility entrypoint for the existing recognition E2E contract."""
    create_service_tls(directory, "recognition")


def configure_recognition_topology(
    config: dict[str, Any], topology: str, workspace: Path
) -> Path | None:
    """Build the E2E fixture overlay; deployment remains the topology compiler."""
    if topology == "local":
        config["recognition"] = {"runtime": "local"}
        return None
    tls_directory = workspace / "recognition-tls"
    create_recognition_tls(tls_directory)
    config["recognition"] = {
        "runtime": "external",
        "endpoint": "recognition:50051",
        "deadline": 5,
        "job_deadline": 30,
        "observation_capacity": 128,
        "control_capacity": 64,
        "outcome_capacity": 128,
        "shutdown_drain": 10,
        "tls": {
            "ca": "/run/recognition-tls/ca.crt",
            "certificate": "/run/recognition-tls/client.crt",
            "key": "/run/recognition-tls/client.key",
            "server_name": "recognition",
        },
    }
    return tls_directory


def configure_tracker_topology(
    config: dict[str, Any], topology: str
) -> Path | None:
    """Build the E2E tracker fixture overlay; launcher compiles its runtime views."""
    if topology != "tracker":
        config.pop("tracker", None)
        return None
    node_id = "edge-local"
    server_name = "tracker-edge-local"
    tls_directory = Path(".tmp/runtime/tracker-tls") / node_id
    create_service_tls(tls_directory, server_name, "frigate-main")
    config["tracker"] = {
        node_id: {
            "managed": True,
            "endpoint": f"{server_name}:50052",
            "cameras": ["face_camera", "car_camera"],
            "deadline": 5,
            "output_capacity": 256,
            "control_capacity": 64,
            "shutdown_drain": 10,
            "evidence": {
                "memory_bytes_per_camera": 32 * 1024 * 1024,
                "ttl": 45,
            },
            "spool": {
                "max_bytes": 256 * 1024 * 1024,
                "retention": 24 * 60 * 60,
            },
            "tls": {
                "ca": "/run/tracker-tls/ca.crt",
                "certificate": "/run/tracker-tls/client.crt",
                "key": "/run/tracker-tls/client.key",
                "server_name": server_name,
            },
        }
    }
    return tls_directory


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


def docker_logs(container: str, since: float) -> str:
    """Read both container streams because Python logging uses stderr."""
    result = subprocess.run(
        ["docker", "logs", "--since", str(int(since)), container],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    return "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )


def capture_container_diagnostics(
    output: Path, since: float | None, topology: str = "local"
) -> None:
    """Persist acceptance-container evidence before restore replaces it."""
    launcher_steps = Path(".tmp/runtime/launcher-steps.jsonl")
    if launcher_steps.is_file():
        shutil.copy2(launcher_steps, output / "launcher-steps.jsonl")
    containers = ["frigate"]
    if topology in ("recognition", "tracker"):
        containers.append("camera-recognition")
    if topology == "tracker":
        containers.append("camera-tracker-edge-local")
    for container in containers:
        suffix = {
            "frigate": "",
            "camera-recognition": "-recognition",
            "camera-tracker-edge-local": "-tracker-edge-local",
        }[container]
        inspect = subprocess.run(
            ["docker", "inspect", container],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        (output / f"container-inspect{suffix}.json").write_text(
            inspect.stdout or inspect.stderr, encoding="utf-8"
        )
        args = ["docker", "logs", container]
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
        (output / f"container{suffix}.log").write_text(
            logs.stdout + logs.stderr, encoding="utf-8"
        )


def restart_counts() -> dict[str, int]:
    names = [
        name
        for name in docker_output("ps", "--format", "{{.Names}}").splitlines()
        if name in {"frigate", "camera-recognition"}
        or name.startswith("camera-replay-")
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


def wait_acceptance_ready(
    expected_image: str | None,
    timeout: float = 32.0,
    topology: str = "local",
) -> None:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    stable_required = float(os.environ.get("CAMERA_READY_STABLE_SECONDS", "2"))
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
            latest_ready = topology == "tracker" or all(
                latest_sample(camera) is not None for camera in CAMERAS.values()
            )
            camera_ready = topology == "tracker" or all(
                status["camera_fps"] > 0 for status in camera_status.values()
            )
            detectors = stats.get("detectors", {})
            detector_ready = topology == "tracker" or (bool(detectors) and all(
                float(value.get("inference_speed", 9999)) < 200
                for value in detectors.values()
            ))
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
            ready = (
                camera_ready
                and latest_ready
                and detector_ready
                and face_ready
                and image_ready
            )
            if ready:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= stable_required:
                    return
            else:
                stable_since = None
            last_reason = (
                f"camera={camera_ready} {camera_status}, latest={latest_ready}, "
                f"detector={detector_ready}, face={face_ready}, image={image_ready}, "
                f"stable_seconds={stable_required}"
            )
        except Exception as exc:
            last_reason = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"acceptance runtime not ready: {last_reason}")


def wait_source_starts(directory: Path, timeout: float = 60.0) -> dict[str, float]:
    """Read the first complete frame timestamp emitted by each direct source."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values: dict[str, float] = {}
        for camera in CAMERAS.values():
            path = directory / f"{camera}.start"
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if not text:
                    continue
                try:
                    values[camera] = float(text)
                except ValueError:
                    continue
        if len(values) == len(CAMERAS):
            return values
        time.sleep(0.05)
    raise TimeoutError("direct MP4 sources did not emit their first frame")


def wait_tracker_ready(
    state_path: Path,
    expected_cameras: set[str],
    timeout: float = 15.0,
    require_cameras: bool = True,
) -> dict[str, Any]:
    """Require tracker service readiness, optionally including camera workers."""
    deadline = time.monotonic() + timeout
    last_reason = "tracker state unavailable"
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            nodes = state.get("tracker_nodes", [])
            ready_cameras = {
                camera.get("camera_id")
                for node in nodes
                for camera in node.get("cameras", [])
                if camera.get("ready")
            }
            healthy = all(
                node.get("health", {}).get("ready")
                and not node.get("health", {}).get("degraded")
                for node in nodes
            )
            if nodes and healthy and (
                not require_cameras or ready_cameras == expected_cameras
            ):
                return state
            last_reason = (
                f"nodes={len(nodes)}, healthy={healthy}, "
                f"ready_cameras={sorted(ready_cameras)}, "
                f"expected={sorted(expected_cameras)}"
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_reason = str(exc)
        time.sleep(0.25)
    raise TimeoutError(f"tracker runtime not ready: {last_reason}")


def wait_source_ends(directory: Path, deadline: float) -> dict[str, float]:
    """Wait for each finite detect input to enqueue its final source frame."""
    while time.monotonic() < deadline:
        values: dict[str, float] = {}
        for camera in CAMERAS.values():
            path = directory / f"{camera}.end"
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8").strip()
            try:
                values[camera] = float(text)
            except ValueError:
                continue
        if len(values) == len(CAMERAS):
            return values
        time.sleep(0.05)
    raise TimeoutError("direct MP4 sources did not reach EOF")


def wait_latest_through(source_ended: dict[str, float], deadline: float) -> None:
    """Wait until processing has published the final queued frame per camera."""
    while time.monotonic() < deadline:
        complete = True
        for camera, frame_time in source_ended.items():
            latest = latest_sample(camera)
            if latest is None or latest[1] + 1e-6 < frame_time:
                complete = False
                break
        if complete:
            return
        time.sleep(0.05)
    raise TimeoutError("direct MP4 final frames were not processed")


def restore_mounts_verified(config: Path) -> bool:
    expected_paths = {
        str(config.resolve()).replace("\\", "/").lower(),
        str(Path(".tmp/runtime/config.main.yml").resolve())
        .replace("\\", "/")
        .lower(),
    }
    expected_suffixes = {
        value[0] + value[2:]
        if len(value) > 2 and value[1] == ":"
        else value
        for value in expected_paths
    }
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
        actual = str((config_mount or {}).get("Source", "")).replace("\\", "/").lower()
        return bool(config_mount) and (
            actual in expected_paths
            or any(actual.endswith(suffix) for suffix in expected_suffixes)
        )
    except Exception:
        return False


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
    def __init__(self, topology: str = "local") -> None:
        self.containers = ["frigate"] + (
            ["camera-recognition"] if topology in ("recognition", "tracker") else []
        )
        if topology == "tracker":
            self.containers.append("camera-tracker-edge-local")
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
        self.recognition: list[dict[str, int]] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                cpu_total = 0.0
                memory_total = 0
                for container in self.containers:
                    usage = docker_output(
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.CPUPerc}}|{{.MemUsage}}",
                        container,
                        timeout=5,
                    )
                    cpu_text, memory_text = usage.split("|", 1)
                    cpu_total += float(cpu_text.rstrip("%"))
                    memory_total += parse_bytes(memory_text.split("/")[0])
                self.cpu_percent.append(cpu_total)
                self.memory_bytes.append(memory_total)
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
                    self.evidence_bytes[camera].append(0)
                embeddings = stats.get("embeddings") or {}
                recognition = embeddings.get("recognition") or {}
                self.evidence_pinned.append(int(recognition.get("evidence_pinned", 0)))
                self.recognition.append(
                    {str(key): int(value) for key, value in recognition.items()}
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
            recognition = embeddings.get("recognition") or {}
            if (
                int(recognition.get("in_flight", 0)) == 0
                and int(recognition.get("sessions", 0)) == 0
                and int(recognition.get("evidence_pinned", 0)) == 0
                and int(recognition.get("writer_depth", 0)) == 0
                and int(recognition.get("queue_depth", 0)) == 0
                and int(recognition.get("outcome_depth", 0)) == 0
            ):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def finalize_finite_source_tracks(records: list[dict[str, Any]]) -> int:
    """Send explicit canonical track ends after an acceptance-only source EOF."""
    tracks = sorted(
        {
            (str(record.get("track_id") or ""), str(record.get("camera") or ""))
            for record in records
            if record.get("stage") == "track_seen"
            and record.get("pipeline") in {"face", "lpr"}
            and record.get("track_id")
            and record.get("camera")
        }
    )
    if not tracks:
        return 0
    code = """
import json, sys, time
from frigate.infrastructure.comms.events_updater import EventEndPublisher

publisher = EventEndPublisher()
time.sleep(0.2)
try:
    for event_id, camera in json.loads(sys.argv[1]):
        publisher.publish((event_id, camera, False))
    time.sleep(0.5)
finally:
    publisher.stop()
"""
    docker_output(
        "exec",
        "frigate",
        "python3",
        "-c",
        code,
        json.dumps(tracks),
        timeout=10,
    )
    return len(tracks)


def observe_round_anchors(
    replays: dict[str, Path], replay_started: dict[str, float], hard_deadline: float
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Observe source progress using the publisher's exact one-shot boundary."""
    durations = {CAMERAS[kind]: replay_duration(path) for kind, path in replays.items()}
    states: dict[str, dict[str, Any]] = {
        camera: {
            "means": [],
            "first_observed_frame": None,
            "last_observed_frame": None,
        }
        for camera in replay_started
    }
    completion_deadline = max(
        replay_started[camera] + durations[camera] - LEAD_SECONDS + 0.8
        for camera in replay_started
    )
    while time.monotonic() < hard_deadline:
        for camera in replay_started:
            sample = latest_sample(camera)
            if sample is None:
                continue
            mean, frame_time = sample
            state = states[camera]
            state["means"].append(round(mean, 2))
            state["means"] = state["means"][-30:]
            if frame_time >= replay_started[camera]:
                if state["first_observed_frame"] is None:
                    state["first_observed_frame"] = frame_time
                state["last_observed_frame"] = frame_time

        if time.time() >= completion_deadline and all(
            state["first_observed_frame"] is not None for state in states.values()
        ):
            break
        time.sleep(0.12)

    anchors: dict[str, list[float]] = {
        camera: [replay_started[camera]] for camera in states
    }
    details: dict[str, Any] = {
        camera: {
            "count": 1,
            "boundary": "publisher_source_start",
            "source_start": replay_started[camera],
            "first_observed_frame": state["first_observed_frame"],
            "last_observed_frame": state["last_observed_frame"],
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
        {**passage, **windows[passage["id"]]}
        for passage in manifest[kind].get("passages", [])
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
        if camera == CAMERAS["lpr"]:
            # LPR acceptance is output-only: the pipeline owns trace identity,
            # and fixture time/bbox must not influence it. Compare only the
            # terminal plate published by the completed runtime trace.
            published = [
                record
                for record in trajectory
                if record.get("stage") == "event_published" and record.get("plate")
            ]
            terminal = max(
                enumerate(published),
                key=lambda item: (
                    float(item[1].get("source_pts", item[1].get("frame_time", 0)) or 0),
                    item[0],
                ),
                default=None,
            )
            representative = (
                normalize_plate(terminal[1].get("plate")) if terminal else ""
            )
            matches = []
            if representative:
                for passage in passages_by_camera.get(camera, []):
                    accepted = {
                        normalize_plate(passage.get("expected_plate")),
                        *(
                            normalize_plate(value)
                            for value in passage.get("accepted_plates", [])
                        ),
                    } - {""}
                    if representative in accepted:
                        matches.append(str(passage["id"]))
            if len(matches) == 1:
                for record in trajectory:
                    record["passage_id"] = matches[0]
                    record["fixture_passage_id"] = matches[0]
            elif len(matches) > 1:
                for record in trajectory:
                    record["association_error"] = "ambiguous_plate_fixture"
                    record["association_candidates"] = sorted(matches)
            continue

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
            key=lambda value: float(
                value.get("source_pts", value.get("frame_time", 0))
            ),
        )
        if found
        else None
    )


def stage_trace(
    records: list[dict[str, Any]], stages: tuple[str, ...]
) -> dict[str, Any]:
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


def trace_metrics(
    records: list[dict[str, Any]], elapsed_seconds: float
) -> dict[str, Any]:
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
    """Describe source-PTS completeness for records tied to an input frame."""
    result: dict[str, Any] = {}
    for camera in sorted({str(record.get("camera")) for record in records}):
        values = sorted(
            float(record["source_pts"])
            for record in records
            if str(record.get("camera")) == camera
            and record.get("source_pts") is not None
        )
        gaps = [current - previous for previous, current in zip(values, values[1:])]
        result[camera] = {
            "records_with_source_pts": len(values),
            "records_without_frame": sum(
                1
                for record in records
                if str(record.get("camera")) == camera
                and record.get("frame_time") is None
            ),
            "max_gap_seconds": round(max(gaps, default=0.0), 6),
            "mean_gap_seconds": round(sum(gaps) / len(gaps), 6) if gaps else None,
            "missing_source_pts": sum(
                1
                for record in records
                if str(record.get("camera")) == camera
                and record.get("frame_time") is not None
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


def pipeline_trace_ids(records: list[dict[str, Any]], pipeline: str) -> list[str]:
    """Return producer-owned raw trace IDs without fixture association."""
    return sorted(
        {
            str(record["trace_id"])
            for record in records
            if record.get("pipeline") == pipeline
            and record.get("trace_id")
            and not str(record["trace_id"]).startswith("detector:")
        }
    )


def face_trace_outcome(records: list[dict[str, Any]]) -> str:
    """Classify execution outcome without treating `unknown` as a failure."""
    attempts = [record for record in records if record.get("stage") == "first_attempt"]
    if not attempts:
        return "not_recognized"
    identities = {
        str(record.get("identity")) for record in attempts if record.get("identity")
    }
    if identities and identities <= {"unknown"}:
        return "recognized_unknown"
    if any(record.get("stage") == "confirmed_result" for record in records):
        return "recognized_known_published"
    return "recognized_known_unpublished"


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
    if not passages:
        face_records = [
            record
            for record in records
            if record.get("pipeline") == "face"
            and not str(record.get("trace_id") or "").startswith("detector:")
        ]
        trace_ids = sorted(
            {
                str(record.get("trace_id"))
                for record in face_records
                if record.get("trace_id")
            }
        )
        records_by_trace: dict[str, list[dict[str, Any]]] = collections.defaultdict(
            list
        )
        for record in face_records:
            trace_id = str(record.get("trace_id") or "")
            if trace_id:
                records_by_trace[trace_id].append(record)
        outcomes = {
            trace_id: face_trace_outcome(trace_records)
            for trace_id, trace_records in records_by_trace.items()
        }
        recognition_completed = sum(
            outcome != "not_recognized" for outcome in outcomes.values()
        )
        confirmed = [
            record
            for record in face_records
            if record.get("stage") == "confirmed_result"
        ]
        return (
            {
                "mode": "raw_trace",
                "passages": [],
                "trace_count": len(trace_ids),
                "track_seen_count": sum(
                    record.get("stage") == "track_seen" for record in face_records
                ),
                "attempt_count": sum(
                    record.get("stage") == "first_attempt" for record in face_records
                ),
                "recognition_publish_count": len(confirmed),
                "recognition_completed_trace_count": recognition_completed,
                "recognition_coverage": recognition_completed / len(trace_ids)
                if trace_ids
                else 0.0,
                "recognized_unknown_trace_count": sum(
                    outcome == "recognized_unknown" for outcome in outcomes.values()
                ),
                "recognized_known_trace_count": sum(
                    outcome.startswith("recognized_known")
                    for outcome in outcomes.values()
                ),
                "not_recognized_trace_count": sum(
                    outcome == "not_recognized" for outcome in outcomes.values()
                ),
                "published_identities": sorted(
                    {
                        str(record.get("identity"))
                        for record in confirmed
                        if record.get("identity")
                    }
                ),
                "accuracy": None,
                "detection_recall": None,
                "precision": None,
                "recall": None,
                "false_passages": None,
            },
            [],
            [],
            [],
            [],
            [],
        )
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
                (expected == "unknown" and not known_confirmed)
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
        events = sorted(
            (
                r
                for r in records
                if r.get("passage_id") == passage["id"]
                and r.get("stage") == "event_published"
            ),
            key=lambda record: float(
                record.get("source_pts", record.get("frame_time", 0)) or 0
            ),
        )
        by_round = {
            round_id: [r for r in events if r.get("round_id") == round_id]
            for round_id in range(1, ROUNDS + 1)
        }
        plates = [
            normalize_plate(event.get("plate"))
            for event in events
            if event.get("plate")
        ]
        representative = plates[-1] if plates else None
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
                "expected_plate": normalize_plate(passage.get("expected_plate")),
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
                    None if exact is not False else "expected_plate_not_returned"
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
    accuracy = exact_tp / len(readable) if readable else None
    passage_recall = detected / len(rows) if rows else 0.0
    passage_precision = (
        detected / (detected + false_passages) if detected + false_passages else 0.0
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
    # Correlation is a producer-owned invariant.  Fixture passage/time/bbox
    # assignment is a reporting view and must never manufacture a mismatch or
    # merge two runtime traces.  A trace is correlated by its own id, source
    # PTS, canonical Event id and media evidence lineage.
    mismatches: list[dict[str, Any]] = []
    lineage: dict[str, set[tuple[str, str, str, str]]] = collections.defaultdict(set)
    for record in records:
        trace_id = str(record.get("trace_id") or "")
        if not trace_id:
            continue
        source_pts = record.get("source_pts", record.get("frame_time"))
        event_id = str(record.get("event_id") or record.get("track_id") or "")
        media_id = str(record.get("evidence_id") or record.get("frame_ref") or "")
        lineage[trace_id].add(
            (str(source_pts), event_id, media_id, str(record.get("pipeline") or ""))
        )
    for trace_id, values in lineage.items():
        # Multiple source frames are expected.  A conflicting producer
        # identity on the same trace is not.
        identity = {(event_id, pipeline) for _, event_id, _, pipeline in values}
        if len(identity) > 1:
            mismatches.append(
                {"trace_id": trace_id, "reason": "producer_lineage_conflict", "values": sorted(identity)}
            )
    seen_publications: set[tuple[str, str, str, float, str]] = set()
    for record in records:
        if record.get("stage") not in {
            "first_attempt",
            "confirmed_result",
            "event_published",
        }:
            continue
        required = ("trace_id", "camera", "track_id", "source_pts")
        missing = [name for name in required if record.get(name) is None]
        # Geometry and media are validated by the canonical evidence validator;
        # correlation itself only consumes producer-owned lineage keys.
        if not record.get("event_id") and not record.get("track_id"):
            missing.append("event_id")
        if missing:
            mismatches.append(
                {
                    "stage": record.get("stage"),
                    "reason": "lineage_missing",
                    "fields": missing,
                }
            )
        publication = (
            str(record.get("stage")),
            str(record.get("pipeline")),
            str(record.get("track_id")),
            float(record.get("source_pts") or 0),
            str(record.get("evidence_id") or record.get("frame_ref") or ""),
        )
        if publication in seen_publications and record.get("stage") in {
            "confirmed_result",
            "event_published",
        }:
            mismatches.append(
                {
                    "stage": record.get("stage"),
                    "reason": "duplicate_publication",
                    "track_id": publication[2],
                    "source_pts": publication[3],
                }
            )
        seen_publications.add(publication)
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


def audit_tracker_lifecycle_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate producer lifecycle order without inventing terminal events."""
    errors: list[str] = []
    sequence_keys: set[tuple[str, str, int]] = set()
    identities: dict[tuple[str, str, str, str], str] = {}
    lifecycles: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows:
        sequence_key = (
            str(row["node_id"]),
            str(row["node_epoch"]),
            int(row["journal_sequence"]),
        )
        if sequence_key in sequence_keys:
            errors.append(f"duplicate_sequence:{sequence_key}")
        sequence_keys.add(sequence_key)
        identity = (
            str(row["node_id"]),
            str(row["camera_id"]),
            str(row["stream_epoch"]),
            str(row.get("track_id") or ""),
        )
        event_id = str(row["event_id"])
        previous_event = identities.setdefault(identity, event_id)
        if previous_event != event_id:
            errors.append(f"identity_reused:{identity}")
        lifecycles[event_id].append(str(row["operation"]).upper())

    for event_id, operations in lifecycles.items():
        if operations.count("START") != 1:
            errors.append(f"start_count:{event_id}:{operations.count('START')}")
        if operations.count("END") != 1:
            errors.append(f"end_count:{event_id}:{operations.count('END')}")
        if operations and operations[0] != "START":
            errors.append(f"first_not_start:{event_id}")
        if operations and operations[-1] != "END":
            errors.append(f"last_not_end:{event_id}")

    return {
        "valid": bool(rows) and not errors,
        "journal_entries": len(rows),
        "event_count": len(lifecycles),
        "active_count": sum(
            bool(operations) and operations[-1] != "END"
            for operations in lifecycles.values()
        ),
        "errors": errors,
    }


def tracker_lifecycle_audit(database_path: str) -> dict[str, Any]:
    code = r"""
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
rows = []
for row in db.execute('''
    select node_id,node_epoch,journal_sequence,camera_id,stream_epoch,
           event_id,operation,payload
    from tracker_journal_entry
    order by node_id,journal_sequence
'''):
    payload = json.loads(row[7] or '{}')
    rows.append({
        'node_id': row[0], 'node_epoch': row[1], 'journal_sequence': row[2],
        'camera_id': row[3], 'stream_epoch': row[4], 'event_id': row[5],
        'operation': row[6], 'track_id': payload.get('track_id'),
    })
event_missing = db.execute('''
    select count(*) from (
        select distinct j.event_id from tracker_journal_entry j
        left join event e on e.id=j.event_id
        where j.operation='END' and e.id is null
    )
''').fetchone()[0]
manifest_missing = db.execute('''
    select count(*) from edge_media_manifest m
    left join event e on e.id=m.event_id where e.id is null
''').fetchone()[0]
print(json.dumps({
    'rows': rows,
    'event_missing': event_missing,
    'manifest_event_missing': manifest_missing,
}))
"""
    output = docker_output(
        "exec", "frigate", "python3", "-c", code, database_path, timeout=10
    )
    raw = json.loads(output)
    result = audit_tracker_lifecycle_rows(raw["rows"])
    result["event_missing"] = int(raw["event_missing"])
    result["manifest_event_missing"] = int(raw["manifest_event_missing"])
    result["valid"] = bool(
        result["valid"]
        and result["event_missing"] == 0
        and result["manifest_event_missing"] == 0
    )
    return result


def api_event(event_id: str) -> dict[str, Any] | None:
    try:
        with urlopen(
            f"http://127.0.0.1:5001/api/events/{event_id}", timeout=2
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def trace_lifecycle_groups(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Return producer-owned recognition lifecycles, never detector observations."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        pipeline = str(record.get("pipeline") or record.get("task") or "")
        trace_id = str(record.get("trace_id") or "")
        if pipeline not in {"lpr", "face"} or not trace_id:
            continue
        if trace_id.startswith("detector:"):
            continue
        groups[(pipeline, trace_id)].append(record)
    return {
        key: trace_records
        for key, trace_records in groups.items()
        if any(
            record.get("source_pts") is not None or record.get("frame_time") is not None
            for record in trace_records
        )
    }


def media_evidence_records(media_root: Path) -> list[dict[str, Any]]:
    """Load producer-owned image evidence so every media trace gets a native clip."""
    records: list[dict[str, Any]] = []
    for pipeline in ("lpr", "face"):
        manifest = media_root / pipeline / "evidence.jsonl"
        if not manifest.is_file():
            continue
        records.extend(
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


def annotate_face_evidence(
    media_root: Path, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Validate and expose producer-owned Face images without deriving new media."""
    result: dict[str, Any] = {
        "eligible_records": 0,
        "annotated_count": 0,
        "missing_bbox": 0,
        "missing_raw": 0,
        "errors": [],
        "records": [],
    }
    resolved_root = media_root.resolve()

    def normalized_box(
        value: Any, width: int, height: int
    ) -> tuple[int, int, int, int] | None:
        if not isinstance(value, list | tuple) or len(value) != 4:
            return None
        try:
            x1, y1, x2, y2 = (int(round(float(item))) for item in value)
        except (TypeError, ValueError):
            return None
        x1 = max(0, min(width - 1, x1))
        y1 = max(0, min(height - 1, y1))
        x2 = max(0, min(width - 1, x2))
        y2 = max(0, min(height - 1, y2))
        return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None

    for source_record in records:
        if (
            source_record.get("pipeline") != "face"
            or source_record.get("stage") != "recognition_attempt_bbox"
        ):
            continue
        result["eligible_records"] += 1
        record = dict(source_record)
        artifact_path = record.get("artifact_path")
        if not artifact_path:
            result["missing_raw"] += 1
            continue
        raw_path = (media_root / str(artifact_path)).resolve()
        if not raw_path.is_relative_to(resolved_root) or not raw_path.is_file():
            result["missing_raw"] += 1
            continue
        raw_hash = sha256(raw_path)
        image = cv2.imread(str(raw_path), cv2.IMREAD_COLOR)
        if image is None:
            result["errors"].append(f"cannot decode {artifact_path}")
            continue
        height, width = image.shape[:2]
        person_box = normalized_box(
            record.get("object_box", record.get("person_box")), width, height
        )
        face_box = normalized_box(
            record.get("detail_box", record.get("face_box")), width, height
        )
        if person_box is None or face_box is None:
            result["missing_bbox"] += 1
            continue
        if sha256(raw_path) != raw_hash:
            result["errors"].append(f"raw artifact changed {artifact_path}")
            continue
        record.update(
            {
                "annotated_artifact_path": str(artifact_path),
                "annotated_artifact_sha256": raw_hash,
                "annotated_artifact_bytes": raw_path.stat().st_size,
                "annotation_mode": "producer_owned_bbox_image",
            }
        )
        result["records"].append(record)
        result["annotated_count"] += 1
    result["valid"] = bool(result["eligible_records"]) and (
        result["annotated_count"] == result["eligible_records"]
        and result["missing_bbox"] == 0
        and result["missing_raw"] == 0
        and not result["errors"]
    )
    return result


def sqlite_trace_media(
    event_ids: list[str], database_path: str
) -> dict[str, dict[str, Any]]:
    """Read canonical Event and overlapping native recording rows in one query."""
    if not event_ids:
        return {}
    code = """
import json, sqlite3, sys
ids = json.loads(sys.argv[1])
db = sqlite3.connect(sys.argv[2])
result = {}
for event_id in ids:
    event = db.execute(
        'select id,camera,start_time,end_time,has_clip from event where id=?',
        (event_id,),
    ).fetchone()
    if event is None:
        continue
    start = float(event[2]) - 0.5
    end = (float(event[3]) if event[3] is not None else float(event[2])) + 0.5
    recordings = db.execute(
        'select id,camera,path,start_time,end_time,duration,segment_size '
        'from recordings where camera=? and end_time>=? and start_time<=? '
        'order by start_time',
        (event[1], start, end),
    ).fetchall()
    result[event_id] = {
        'event': {
            'id': event[0], 'camera': event[1], 'start_time': event[2],
            'end_time': event[3], 'has_clip': bool(event[4]),
        },
        'recordings': [
            {
                'id': row[0], 'camera': row[1], 'path': row[2],
                'start_time': row[3], 'end_time': row[4],
                'duration': row[5], 'segment_size': row[6],
            }
            for row in recordings
        ],
    }
print(json.dumps(result))
"""
    value = docker_output(
        "exec",
        "frigate",
        "python3",
        "-c",
        code,
        json.dumps(event_ids),
        database_path,
        timeout=15,
    )
    return json.loads(value or "{}")


def sqlite_recordings_for_trace_ranges(
    ranges: list[dict[str, Any]], database_path: str
) -> dict[str, list[dict[str, Any]]]:
    """Resolve native recordings by producer camera and trace timestamps."""
    if not ranges:
        return {}
    code = """
import json, sqlite3, sys
ranges = json.loads(sys.argv[1])
db = sqlite3.connect(sys.argv[2])
result = {}
for item in ranges:
    rows = db.execute(
        'select id,camera,path,start_time,end_time,duration,segment_size '
        'from recordings where camera=? and end_time>=? and start_time<=? '
        'order by start_time',
        (item['camera'], item['start_time'], item['end_time']),
    ).fetchall()
    result[item['key']] = [
        {
            'id': row[0], 'camera': row[1], 'path': row[2],
            'start_time': row[3], 'end_time': row[4],
            'duration': row[5], 'segment_size': row[6],
        }
        for row in rows
    ]
print(json.dumps(result))
"""
    value = docker_output(
        "exec",
        "frigate",
        "python3",
        "-c",
        code,
        json.dumps(ranges),
        database_path,
        timeout=15,
    )
    return json.loads(value or "{}")


def recordings_available_through(database_path: str) -> dict[str, float]:
    """Return the latest committed native recording timestamp per camera."""
    code = (
        "import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); "
        "print(json.dumps({r[0]:r[1] for r in c.execute("
        "'select camera,max(end_time) from recordings group by camera').fetchall()}))"
    )
    value = docker_output(
        "exec", "frigate", "python3", "-c", code, database_path, timeout=5
    )
    return {
        str(key): float(item)
        for key, item in json.loads(value or "{}").items()
        if item is not None
    }


def wait_recordings_through(
    database_path: str,
    cameras: list[str],
    target_time: float,
    timeout: float = 15.0,
) -> tuple[bool, dict[str, float]]:
    """Wait for Frigate's own recording maintainer to commit the measured window."""
    deadline = time.monotonic() + timeout
    latest: dict[str, float] = {}
    while time.monotonic() < deadline:
        try:
            latest = recordings_available_through(database_path)
            if all(latest.get(camera, 0.0) >= target_time for camera in cameras):
                return True, latest
        except Exception:
            pass
        time.sleep(0.25)
    return False, latest


def ffprobe_clip(path: Path) -> dict[str, Any]:
    """Inspect a downloaded native clip without modifying it."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "format=duration,size:stream=codec_name,width,height,avg_frame_rate,nb_read_frames",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return {"valid": False, "error": result.stderr.strip() or "ffprobe_failed"}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"valid": False, "error": "ffprobe_invalid_json"}
    streams = value.get("streams") or []
    duration = float((value.get("format") or {}).get("duration") or 0.0)
    frame_count = sum(int(stream.get("nb_read_frames") or 0) for stream in streams)
    return {"valid": bool(streams) and duration > 0 and frame_count > 0, **value}


def collect_native_trace_clips(
    output: Path,
    records: list[dict[str, Any]],
    database_path: str,
    *,
    api_base: str = "http://127.0.0.1:5001",
    edge_owned: bool = False,
) -> dict[str, Any]:
    """Download trace clips from Frigate's native recording API.

    Fixture files and replay anchors are intentionally not accepted by this
    interface, making reverse materialization impossible.
    """
    groups = trace_lifecycle_groups(records)
    traces: list[dict[str, Any]] = []
    resolved_events: dict[tuple[str, str], dict[str, Any]] = {}

    # Event finalization is asynchronous.  Resolve only the exact producer
    # track id and fail closed when a lifecycle has zero or multiple Events.
    deadline = time.monotonic() + 8.0
    while True:
        pending = False
        for key, trace_records in groups.items():
            if key in resolved_events:
                continue
            camera = str(trace_records[0].get("camera") or "")
            event_ids = sorted(
                {
                    str(record.get("event_id") or record.get("track_id"))
                    for record in trace_records
                    if record.get("event_id") or record.get("track_id")
                }
            )
            matches = [
                event
                for event_id in event_ids
                if (event := api_event(event_id)) is not None
                and str(event.get("camera")) == camera
            ]
            if len(matches) == 1 and matches[0].get("end_time") is not None:
                resolved_events[key] = matches[0]
            else:
                pending = True
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    event_ids = sorted(
        {str(event["id"]) for event in resolved_events.values() if event.get("id")}
    )
    media_rows = sqlite_trace_media(event_ids, database_path)
    trace_ranges: dict[tuple[str, str], tuple[float, float]] = {}
    range_queries: list[dict[str, Any]] = []
    for key, trace_records in groups.items():
        trace_times = []
        for record in trace_records:
            value = record.get("source_pts")
            if value is None:
                value = record.get("frame_time")
            if value is not None:
                trace_times.append(float(value))
        if not trace_times:
            continue
        start_time = min(trace_times) - 0.25
        end_time = max(trace_times) + 0.25
        trace_ranges[key] = (start_time, end_time)
        range_queries.append(
            {
                "key": f"{key[0]}\0{key[1]}",
                "camera": str(trace_records[0].get("camera") or ""),
                "start_time": start_time,
                "end_time": end_time,
            }
        )
    recordings_by_range = sqlite_recordings_for_trace_ranges(
        range_queries, database_path
    )
    for (pipeline, trace_id), trace_records in sorted(groups.items()):
        safe_trace = re.sub(r"[^A-Za-z0-9_.-]+", "_", trace_id)
        trace_dir = output / "media" / pipeline / safe_trace
        trace_dir.mkdir(parents=True, exist_ok=True)
        camera = str(trace_records[0].get("camera") or "")
        event = resolved_events.get((pipeline, trace_id))
        metadata: dict[str, Any] = {
            "trace_id": trace_id,
            "pipeline": pipeline,
            "camera": camera,
            "event_id": None,
            "clip_status": "missing",
            "clip_reason": "trace_timestamp_missing",
            "clip_path": None,
            "recordings": [],
            "record_count": len(trace_records),
        }
        recognition_history = []
        for record in trace_records:
            stage = str(record.get("stage") or "")
            if stage not in {
                "ocr_result",
                "event_published",
                "first_attempt",
                "confirmed_result",
                "recognition_failed",
                "recognition_skipped",
            }:
                continue
            recognition_history.append(
                {
                    "stage": stage,
                    "frame_time": record.get("frame_time"),
                    "value": record.get("plate", record.get("identity")),
                    "score": record.get("score"),
                    "reason": record.get("reason"),
                }
            )
        published_stages = {"event_published", "confirmed_result"}
        final_result = next(
            (
                item
                for item in reversed(recognition_history)
                if item["stage"] in published_stages
            ),
            recognition_history[-1] if recognition_history else None,
        )
        metadata["recognition"] = {
            "status": (
                "published"
                if final_result and final_result["stage"] in published_stages
                else "skipped"
                if final_result and final_result["stage"] == "recognition_skipped"
                else "observed"
                if final_result
                else "not_recognized"
            ),
            "final_result": final_result,
            "history": recognition_history,
        }
        range_key = f"{pipeline}\0{trace_id}"
        recordings: list[dict[str, Any]] = recordings_by_range.get(range_key, [])
        start_time: float | None = None
        end_time: float | None = None
        if (pipeline, trace_id) in trace_ranges:
            start_time, end_time = trace_ranges[(pipeline, trace_id)]
            metadata.update(
                {
                    "clip_basis": "trace_lifecycle",
                    "clip_start_time": start_time,
                    "clip_end_time": end_time,
                    "recordings": recordings,
                    "clip_reason": None if recordings else "recording_coverage_missing",
                }
            )
        if event is not None:
            event_id = str(event["id"])
            sqlite_media = media_rows.get(event_id, {})
            source_event = sqlite_media.get("event") or {}
            metadata.update(
                {
                    "event_id": event_id,
                    "event_start_time": event.get("start_time"),
                    "event_end_time": event.get("end_time"),
                    "event_has_clip_api": bool(event.get("has_clip")),
                    "event_has_clip_sqlite": bool(source_event.get("has_clip")),
                }
            )
        media_url = None
        if edge_owned and event is not None:
            media_url = f"{api_base}/api/events/{quote(str(event['id']), safe='')}/clip.mp4"
            metadata.update(
                {
                    "clip_basis": "edge_media_manifest",
                    "clip_reason": None,
                }
            )
        elif recordings:
            assert start_time is not None and end_time is not None
            coverage_start = min(float(row["start_time"]) for row in recordings)
            coverage_end = max(float(row["end_time"]) for row in recordings)
            request_start = max(start_time, coverage_start)
            request_end = min(end_time, coverage_end)
            # Frigate's recording API emits whole encoded-frame groups. A
            # sub-second edge window can contain the trace timestamp yet still
            # return only an MP4 header, so retain at least one second of the
            # same native recording when coverage permits.
            request_end = min(coverage_end, max(request_end, request_start + 1.0))
            metadata.update(
                {
                    "recording_coverage_start": coverage_start,
                    "recording_coverage_end": coverage_end,
                    "clip_request_start": request_start,
                    "clip_request_end": request_end,
                }
            )
            if request_end <= request_start:
                metadata["clip_reason"] = "recording_coverage_missing"
                recordings = []
        if recordings and media_url is None:
            media_url = (
                f"{api_base}/api/{quote(camera, safe='')}/start/"
                f"{request_start:.6f}/end/{request_end:.6f}/clip.mp4"
            )
        if media_url is not None:
            try:
                with urlopen(media_url, timeout=45) as response:
                    payload = response.read()
                    content_type = response.headers.get("Content-Type", "")
                if not payload:
                    raise RuntimeError("native clip response was empty")
                target = trace_dir / "clip.mp4"
                target.write_bytes(payload)
                probe = ffprobe_clip(target)
                metadata.update(
                    {
                        "clip_status": "recorded" if probe.get("valid") else "invalid",
                        "clip_reason": None
                        if probe.get("valid")
                        else "ffprobe_invalid",
                        "clip_path": target.relative_to(output).as_posix(),
                        "clip_bytes": len(payload),
                        "clip_sha256": sha256(target),
                        "content_type": content_type,
                        "ffprobe": probe,
                    }
                )
                if edge_owned and event is not None:
                    range_request = Request(
                        media_url, headers={"Range": "bytes=0-1023"}
                    )
                    with urlopen(range_request, timeout=15) as response:
                        range_payload = response.read()
                        range_status = response.status
                        content_range = response.headers.get("Content-Range", "")
                    snapshot_url = (
                        f"{api_base}/api/events/"
                        f"{quote(str(event['id']), safe='')}/snapshot.jpg"
                    )
                    with urlopen(snapshot_url, timeout=15) as response:
                        snapshot_payload = response.read()
                        snapshot_type = response.headers.get("Content-Type", "")
                    metadata.update(
                        {
                            "range_proxy_status": range_status,
                            "range_proxy_bytes": len(range_payload),
                            "range_proxy_content_range": content_range,
                            "snapshot_proxy_bytes": len(snapshot_payload),
                            "snapshot_proxy_content_type": snapshot_type,
                            "media_proxy_complete": (
                                range_status == 206
                                and bool(range_payload)
                                and content_range.startswith("bytes 0-")
                                and bool(snapshot_payload)
                                and snapshot_type.startswith("image/jpeg")
                            ),
                        }
                    )
            except Exception as exc:
                metadata["clip_reason"] = (
                    f"native_clip_error:{type(exc).__name__}:{exc}"
                )
        write_json(trace_dir / "trace.json", metadata)
        traces.append(metadata)

    result = {
        "mode": "frigate_native_recording",
        "trace_count": len(traces),
        "recorded_count": sum(item["clip_status"] == "recorded" for item in traces),
        "missing_count": sum(item["clip_status"] != "recorded" for item in traces),
        "complete": bool(traces)
        and all(item["clip_status"] == "recorded" for item in traces),
        "media_proxy_complete": not edge_owned
        or (
            bool(traces)
            and all(item.get("media_proxy_complete") for item in traces)
        ),
        "traces": traces,
    }
    write_json(output / "native-media.json", result)
    return result


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
    lpr = [p for p in manifest["lpr"]["passages"] if p.get("valid_passage", True)]
    result = {
        "face_mode": "raw_trace",
        "face_source": manifest["face"].get("source"),
        "face_passages": len(manifest["face"].get("passages", [])),
        "vehicle_passages": len(lpr),
        "readable_vehicle_passages": sum(bool(p.get("readable")) for p in lpr),
    }
    result["valid"] = (
        bool(result["face_source"])
        and result["face_passages"] == 0
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
        "| " + " | ".join(_md_value(value) for value in row) + " |" for row in rows
    )
    return "\n".join(lines) if rows else "_Không có dữ liệu._"


def _md_thumbnail(relative: str, alt: str, width: int = 240) -> str:
    """Render a bounded preview that links to the original producer artifact."""
    return (
        f'<a href="{relative}"><img src="{relative}" alt="{alt}" '
        f'width="{width}" loading="lazy"></a>'
    )


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
        "### Raw trace inventory",
        "",
        _md_table(
            ["Pipeline", "Raw trace IDs read", "Recognition-completed traces"],
            [
                [
                    "LPR",
                    lpr.get("raw_trace_count"),
                    lpr.get("recognized_trace_count"),
                ],
                [
                    "Face",
                    face.get("trace_count"),
                    face.get("recognition_completed_trace_count"),
                ],
            ],
        ),
        "",
        "## 2. Hardware và recognition writer metrics",
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
                ["Writer depth", runtime.get("recognition", {}).get("writer_depth")],
                ["Writer drops", runtime.get("recognition", {}).get("writer_drops")],
                ["Writer errors", runtime.get("recognition", {}).get("writer_errors")],
                ["Cleanup zero", runtime.get("recognition", {}).get("cleanup_zero")],
                [
                    "Sampler errors",
                    "; ".join(runtime.get("hardware_sampler_errors", [])),
                ],
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
            [
                "Camera",
                "Records with PTS",
                "Max gap (s)",
                "Mean gap (s)",
                "Missing PTS",
            ],
            [
                [
                    camera,
                    values.get("records_with_source_pts"),
                    values.get("max_gap_seconds"),
                    values.get("mean_gap_seconds"),
                    values.get("missing_source_pts"),
                ]
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
        path
        for path in output.rglob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    image_rows = []
    for path in image_paths:
        relative = path.relative_to(output).as_posix()
        image_rows.append([f"![{path.name}]({relative})", f"`{relative}`"])
    lines.extend(
        [
            _md_table(["Preview", "Artifact"], image_rows),
            "",
            "## 8. JSON/log artifacts",
            "",
        ]
    )
    artifact_rows = []
    for artifact in summary.get("report", {}).get("artifacts", []):
        name = artifact.get("path", "")
        artifact_rows.append(
            [f"[{name}]({name})", artifact.get("bytes"), artifact.get("sha256")]
        )
    lines.append(_md_table(["Artifact", "Bytes", "SHA-256"], artifact_rows))
    lines.extend(
        [
            "",
            "## 9. Diagnostic notes",
            "",
            "- Đây là báo cáo quan sát; không có tiêu chí pass/fail.",
        ]
    )
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
    failed_face = [
        row
        for row in summary.get("face", {}).get("passages", [])
        if not row.get("correct")
    ]
    lpr_expectations = summary.get("lpr", {}).get("passages", [])
    expected_by_plate = {
        normalize_plate(row.get("expected_plate")): str(row.get("passage_id"))
        for row in lpr_expectations
        if normalize_plate(row.get("expected_plate"))
    }

    trace_path = output / "runtime-trace.json"
    trace_records = (
        json.loads(trace_path.read_text(encoding="utf-8")).get("records", [])
        if trace_path.is_file()
        else []
    )
    evidence_path = output / "runtime-evidence.json"
    evidence_records = (
        json.loads(evidence_path.read_text(encoding="utf-8")).get("records", [])
        if evidence_path.is_file()
        else []
    )
    annotated_path = output / "face-annotated-evidence.json"
    annotated_records = (
        json.loads(annotated_path.read_text(encoding="utf-8")).get("records", [])
        if annotated_path.is_file()
        else []
    )

    lpr_records_by_trace: dict[str, list[dict[str, Any]]] = collections.defaultdict(
        list
    )
    for record in [*trace_records, *evidence_records, *annotated_records]:
        if record.get("pipeline") != "lpr":
            continue
        trace_id = str(record.get("trace_id") or record.get("track_id") or "")
        if not trace_id or trace_id.startswith("detector:"):
            continue
        lpr_records_by_trace[trace_id].append(record)

    lpr_trace_rows: list[dict[str, Any]] = []
    for trace_id, records in sorted(lpr_records_by_trace.items()):
        published = sorted(
            (
                record
                for record in records
                if record.get("stage") == "event_published" and record.get("plate")
            ),
            key=lambda record: float(
                record.get("source_pts", record.get("frame_time", 0)) or 0
            ),
        )
        final_plate = normalize_plate(published[-1].get("plate")) if published else ""
        fixture_match = expected_by_plate.get(final_plate)
        stages: dict[str, dict[str, Any]] = {}
        for record in sorted(
            records,
            key=lambda item: float(
                item.get("source_pts", item.get("frame_time", 0)) or 0
            ),
        ):
            stages[str(record.get("stage"))] = record
        lpr_trace_rows.append(
            {
                "trace_id": trace_id,
                "records": records,
                "stages": stages,
                "final_plate": final_plate,
                "fixture_match": fixture_match,
                "comparison": (
                    "MATCH"
                    if fixture_match
                    else ("UNEXPECTED" if final_plate else "NO_OUTPUT")
                ),
            }
        )

    failed_lpr_traces = [row for row in lpr_trace_rows if row["comparison"] != "MATCH"]
    face_records_by_trace: dict[str, list[dict[str, Any]]] = collections.defaultdict(
        list
    )
    for record in [*trace_records, *evidence_records, *annotated_records]:
        if record.get("pipeline") != "face":
            continue
        trace_id = str(record.get("trace_id") or record.get("track_id") or "")
        if not trace_id or trace_id.startswith("detector:"):
            continue
        face_records_by_trace[trace_id].append(record)
    face_trace_rows: list[dict[str, Any]] = []
    face_required_stages = (
        "track_seen",
        "first_qualified_face",
        "candidate_submitted",
        "first_attempt",
        "confirmed_result",
    )
    for trace_id, records in sorted(face_records_by_trace.items()):
        confirmed = [
            record for record in records if record.get("stage") == "confirmed_result"
        ]
        observed_stages = {
            str(record.get("stage")) for record in records if record.get("stage")
        }
        attempts = [
            record for record in records if record.get("stage") == "first_attempt"
        ]
        attempt_identities = sorted(
            {
                str(record.get("identity"))
                for record in attempts
                if record.get("identity")
            }
        )
        attempt_scores = [
            float(record["score"])
            for record in attempts
            if record.get("score") is not None
        ]
        if "first_qualified_face" not in observed_stages:
            recognition_detail = "no_qualified_face"
        elif not attempts:
            recognition_detail = "classifier_returned_no_result"
        else:
            recognition_detail = face_trace_outcome(records)
        face_trace_rows.append(
            {
                "trace_id": trace_id,
                "records": records,
                "track_seen": sum(
                    record.get("stage") == "track_seen" for record in records
                ),
                "attempts": len(attempts),
                "attempt_identities": attempt_identities,
                "attempt_score_min": min(attempt_scores, default=None),
                "attempt_score_max": max(attempt_scores, default=None),
                "confirmed": len(confirmed),
                "outcome": face_trace_outcome(records),
                "recognition_detail": recognition_detail,
                "observed_stages": sorted(observed_stages),
                "missing_stages": [
                    stage
                    for stage in face_required_stages
                    if stage not in observed_stages
                ],
                "identities": ", ".join(
                    sorted(
                        {
                            str(record.get("identity"))
                            for record in confirmed
                            if record.get("identity")
                        }
                    )
                )
                or "-",
            }
        )
    failed_face_trace_rows = [
        row for row in face_trace_rows if row["outcome"] == "not_recognized"
    ]
    successful_face_trace_rows = [
        row for row in face_trace_rows if row["outcome"] == "recognized_known_published"
    ]

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    group_passages: dict[tuple[str, str], str] = {}
    detector_records: dict[tuple[str, str], list[dict[str, Any]]] = (
        collections.defaultdict(list)
    )

    def add_records(kind: str, passage_id: str, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record.get("pipeline") == "detector" or str(
                record.get("trace_id") or ""
            ).startswith("detector:"):
                detector_records[(kind, passage_id)].append(record)
                continue
            trace_id = str(record.get("trace_id") or record.get("track_id") or "")
            if not trace_id:
                continue
            key = (kind, trace_id)
            groups.setdefault(key, []).append(record)
            group_passages[key] = passage_id

    for row in failed_lpr_traces:
        add_records(
            "LPR",
            str(row.get("fixture_match") or "-"),
            row.get("records", []),
        )
    for row in failed_face:
        for round_data in row.get("rounds", []):
            trace = round_data.get("trace") or {}
            add_records("Face", str(row.get("passage_id")), trace.get("records", []))
    for row in failed_face_trace_rows:
        trace_id = str(row["trace_id"])
        groups[("Face", trace_id)] = list(row["records"])
        group_passages[("Face", trace_id)] = "raw_trace"
    for key, records in groups.items():
        passage_id = group_passages.get(key, "")
        records.extend(detector_records.get((key[0], passage_id), []))

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

    def clip_href(kind: str, trace_id: str) -> str:
        if trace_id == "-":
            return "-"
        pipeline = kind.lower()
        safe_trace = re.sub(r"[^A-Za-z0-9_.-]+", "_", trace_id)
        trace_clip = output / "media" / pipeline / safe_trace / "clip.mp4"
        if trace_clip.is_file():
            return f"media/{pipeline}/{safe_trace}/clip.mp4"
        return "-"

    def annotated_face_href(records: list[dict[str, Any]]) -> str:
        for record in records:
            relative = record.get("annotated_artifact_path")
            if relative and (output / "media" / str(relative)).is_file():
                return f"media/{Path(str(relative)).as_posix()}"
        return "-"

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
                    record
                    for record in evidence_records
                    if str(record.get("passage_id")) == passage_id
                )
            else:
                for round_data in passage.get("rounds", []):
                    source_records.extend(
                        (round_data.get("trace") or {}).get("records", [])
                    )
            trace_ids = sorted(
                {
                    str(record.get("trace_id"))
                    for record in source_records
                    if record.get("trace_id")
                    and record.get("pipeline") != "detector"
                    and not str(record.get("trace_id")).startswith("detector:")
                }
            ) or ["-"]
            for trace_id in trace_ids:
                trace_records = [
                    record
                    for record in source_records
                    if str(record.get("trace_id")) == trace_id
                ]
                by_stage: dict[str, dict[str, Any]] = {}
                for record in trace_records:
                    by_stage.setdefault(str(record.get("stage")), record)
                if "detector_hit" not in by_stage:
                    detector = next(
                        (
                            record
                            for record in source_records
                            if record.get("stage") == "detector_hit"
                        ),
                        None,
                    )
                    if detector is not None:
                        by_stage["detector_hit"] = detector
                rows.append(
                    [
                        kind,
                        passage_id,
                        trace_id,
                        f"[clip]({clip_href(kind, trace_id)})"
                        if clip_href(kind, trace_id) != "-"
                        else "-",
                        passage.get("mismatch_reason")
                        or (
                            "recognition_not_correct"
                            if kind == "Face"
                            else "not_detected_or_not_exact"
                        ),
                        *[failure_value(by_stage.get(stage)) for stage in required],
                    ]
                )
        return rows

    lines = [
        "# Runtime Test Report",
        "",
        f"- **Run:** `{output.name}`",
        f"- **Status:** `{summary.get('report', {}).get('status', '-')}`",
        f"- **Measurement valid:** `{summary.get('measurement', {}).get('measurement_valid', '-')}`",
        "",
        "Trace LPR do pipeline sở hữu. Fixture chỉ được dùng để đối chiếu biển số cuối cùng; không dùng time/bbox để gán trace hoặc suy diễn stage.",
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
                [
                    "Native clips",
                    f"{runtime.get('native_media', {}).get('recorded_count', 0)} / {runtime.get('native_media', {}).get('trace_count', 0)}",
                ],
                [
                    "Native clip evidence complete",
                    runtime.get("native_media", {}).get("complete"),
                ],
                [
                    "Face annotated evidence",
                    f"{runtime.get('face_annotated_evidence', {}).get('annotated_count', 0)} / "
                    f"{runtime.get('face_annotated_evidence', {}).get('eligible_records', 0)}",
                ],
            ],
        ),
        "",
        "## Hardware / recognition writer performance",
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
                ["Writer depth", runtime.get("recognition", {}).get("writer_depth")],
                ["Writer drops", runtime.get("recognition", {}).get("writer_drops")],
                ["Writer errors", runtime.get("recognition", {}).get("writer_errors")],
                ["Cleanup zero", runtime.get("recognition", {}).get("cleanup_zero")],
                [
                    "Sampler errors",
                    "; ".join(runtime.get("hardware_sampler_errors", [])),
                ],
            ],
        ),
        "",
        "## Result summary",
        "",
        _md_table(
            [
                "Pipeline",
                "Recall",
                "Precision",
                "Accuracy",
                "Exact match",
                "Observed / expected",
            ],
            [
                [
                    "LPR",
                    summary.get("lpr", {}).get("recall"),
                    summary.get("lpr", {}).get("precision"),
                    summary.get("lpr", {}).get("accuracy"),
                    summary.get("lpr", {}).get("exact_match"),
                    f"{sum(bool(row.get('fixture_match')) for row in lpr_trace_rows)} / {len(lpr_expectations)}",
                ],
                [
                    "Face",
                    summary.get("face", {}).get("recall"),
                    summary.get("face", {}).get("precision"),
                    summary.get("face", {}).get("accuracy"),
                    "raw trace",
                    f"{summary.get('face', {}).get('trace_count', 0)} tracks / {summary.get('face', {}).get('attempt_count', 0)} attempts",
                ],
            ],
        ),
        "",
        "## Face library snapshot",
        "",
        "Face replay dùng trực tiếp clip cố định `01_P1E_S1_C1_5s-20s.mp4`; fixture không cắt hoặc chuyển mã video lúc chạy.",
        "",
        "Acceptance sao chép read-only các identity đã cấu hình sang media cô lập; không sao chép `train` và không tạo enrollment tổng hợp.",
        "",
        _md_table(
            ["Identity", "Images"],
            [
                [identity, value.get("image_count")]
                for identity, value in sorted(
                    summary.get("fixture", {})
                    .get("face_library_snapshot", {})
                    .get("identities", {})
                    .items()
                )
            ],
        ),
        "",
        f"Library total: `{summary.get('fixture', {}).get('face_library_snapshot', {}).get('identity_count', 0)}` identities / `{summary.get('fixture', {}).get('face_library_snapshot', {}).get('image_count', 0)}` images; SHA-256 `{summary.get('fixture', {}).get('face_library_snapshot', {}).get('sha256', '-')}`.",
        "",
        "## Raw trace inventory",
        "",
        "Đếm trực tiếp producer-owned `trace_id`; không dùng fixture passage association để gộp trace.",
        "",
        _md_table(
            [
                "Pipeline",
                "Raw trace IDs read",
                "track_seen records",
                "Recognition-completed traces",
                "Unknown traces",
                "Not-recognized traces",
                "Published records",
                "Media trace folders",
            ],
            [
                [
                    "LPR",
                    len(lpr_trace_rows),
                    sum(
                        record.get("stage") == "track_seen"
                        for records in lpr_records_by_trace.values()
                        for record in records
                    ),
                    sum(
                        any(record.get("stage") == "ocr_result" for record in records)
                        for records in lpr_records_by_trace.values()
                    ),
                    "-",
                    sum(
                        not any(
                            record.get("stage") == "ocr_result" for record in records
                        )
                        for records in lpr_records_by_trace.values()
                    ),
                    sum(
                        record.get("stage") == "event_published"
                        for records in lpr_records_by_trace.values()
                        for record in records
                    ),
                    len(list((output / "media" / "lpr").glob("lpr_*")))
                    if (output / "media" / "lpr").is_dir()
                    else 0,
                ],
                [
                    "Face",
                    len(face_trace_rows),
                    sum(row["track_seen"] for row in face_trace_rows),
                    sum(row["outcome"] != "not_recognized" for row in face_trace_rows),
                    sum(
                        row["outcome"] == "recognized_unknown"
                        for row in face_trace_rows
                    ),
                    sum(row["outcome"] == "not_recognized" for row in face_trace_rows),
                    sum(row["confirmed"] for row in face_trace_rows),
                    len(list((output / "media" / "face").glob("face_*")))
                    if (output / "media" / "face").is_dir()
                    else 0,
                ],
            ],
        ),
        "",
        "## Stage throughput",
        "",
        _md_table(
            ["Pipeline", "Stage", "Count", "Calls/s"],
            [
                [
                    "Runtime",
                    stage,
                    count,
                    trace_metrics_data.get("stage_calls_per_second", {}).get(stage),
                ]
                for stage, count in sorted(
                    trace_metrics_data.get("stage_counts", {}).items()
                )
            ]
            + [
                [
                    "LPR evidence",
                    stage,
                    count,
                    lpr_trace_metrics.get("stage_calls_per_second", {}).get(stage),
                ]
                for stage, count in sorted(
                    lpr_trace_metrics.get("stage_counts", {}).items()
                )
            ],
        ),
        "",
        "## Source PTS",
        "",
        _md_table(
            ["Camera", "Records", "Max gap (s)", "Mean gap (s)", "Missing"],
            [
                [
                    camera,
                    value.get("records_with_source_pts"),
                    value.get("max_gap_seconds"),
                    value.get("mean_gap_seconds"),
                    value.get("missing_source_pts"),
                ]
                for camera, value in sorted(runtime.get("source_pts", {}).items())
            ],
        ),
        "",
        "## Trace summary",
        "",
        "### LPR",
        "",
        _md_table(
            [
                "trace_id",
                "Clip",
                "Final plate",
                "Fixture match",
                "Comparison",
                "track_seen",
                "lpr_eligible",
                "plate_detector_result",
                "ocr_result",
                "event_published",
            ],
            [
                [
                    row["trace_id"],
                    f"[clip]({clip_href('LPR', row['trace_id'])})"
                    if clip_href("LPR", row["trace_id"]) != "-"
                    else "-",
                    row["final_plate"] or "-",
                    row["fixture_match"] or "-",
                    row["comparison"],
                    *[
                        failure_value(row["stages"].get(stage))
                        for stage in (
                            "track_seen",
                            "lpr_eligible",
                            "plate_detector_result",
                            "ocr_result",
                            "event_published",
                        )
                    ],
                ]
                for row in lpr_trace_rows
            ],
        ),
        "",
        "### LPR plate comparison",
        "",
        _md_table(
            [
                "Fixture",
                "Expected plate",
                "Runtime trace",
                "Returned plate",
                "Comparison",
            ],
            [
                [
                    expectation.get("passage_id"),
                    expectation.get("expected_plate") or "-",
                    next(
                        (
                            row["trace_id"]
                            for row in lpr_trace_rows
                            if row.get("fixture_match") == expectation.get("passage_id")
                        ),
                        "-",
                    ),
                    next(
                        (
                            row["final_plate"]
                            for row in lpr_trace_rows
                            if row.get("fixture_match") == expectation.get("passage_id")
                        ),
                        "-",
                    ),
                    "MATCH"
                    if any(
                        row.get("fixture_match") == expectation.get("passage_id")
                        for row in lpr_trace_rows
                    )
                    else "MISSING",
                ]
                for expectation in lpr_expectations
            ],
        ),
        "",
        "### Face",
        "",
        _md_table(
            [
                "trace_id",
                "Clip",
                "BBox evidence",
                "track_seen",
                "Attempts",
                "Confirmed",
                "Published identities",
            ],
            [
                [
                    row["trace_id"],
                    f"[clip]({clip_href('Face', str(row['trace_id']))})"
                    if clip_href("Face", str(row["trace_id"])) != "-"
                    else "-",
                    f"[image]({annotated_face_href(row['records'])})"
                    if annotated_face_href(row["records"]) != "-"
                    else "-",
                    row["track_seen"],
                    row["attempts"],
                    row["confirmed"],
                    row["identities"],
                ]
                for row in face_trace_rows
            ],
        ),
        "",
        "Face dùng raw tracker lineage; fixture không gán passage, bbox hoặc expected identity.",
        "",
        "### Published known-identity Face trace index",
        "",
        _md_table(
            [
                "trace_id",
                "Clip",
                "Attempts",
                "Confirmed publications",
                "Published identities",
                "Attempt score range",
            ],
            [
                [
                    row["trace_id"],
                    f"[clip]({clip_href('Face', str(row['trace_id']))})"
                    if clip_href("Face", str(row["trace_id"])) != "-"
                    else "-",
                    row["attempts"],
                    row["confirmed"],
                    row["identities"],
                    f"{row['attempt_score_min']} .. {row['attempt_score_max']}",
                ]
                for row in successful_face_trace_rows
            ],
        ),
        "",
        "### Face recognition outcome index",
        "",
        "`recognized_unknown` nghĩa là inference đã chạy và model trả `unknown`; đây là recognition hợp lệ, không phải failure.",
        "",
        _md_table(
            [
                "trace_id",
                "Clip",
                "BBox evidence",
                "Recognition outcome",
                "Detail",
                "Attempts",
                "Attempt identities",
                "Attempt score range",
                "Observed stages",
                "Missing stages",
            ],
            [
                [
                    row["trace_id"],
                    f"[clip]({clip_href('Face', str(row['trace_id']))})"
                    if clip_href("Face", str(row["trace_id"])) != "-"
                    else "-",
                    f"[image]({annotated_face_href(row['records'])})"
                    if annotated_face_href(row["records"]) != "-"
                    else "-",
                    row["outcome"],
                    row["recognition_detail"],
                    row["attempts"],
                    ", ".join(row["attempt_identities"]) or "-",
                    f"{row['attempt_score_min']} .. {row['attempt_score_max']}",
                    ", ".join(row["observed_stages"]),
                    ", ".join(row["missing_stages"]) or "-",
                ]
                for row in face_trace_rows
            ],
        ),
        "",
        "### Not-recognized Face lifecycle index",
        "",
        _md_table(
            ["trace_id", "Clip", "Detail", "Observed stages", "Missing stages"],
            [
                [
                    row["trace_id"],
                    f"[clip]({clip_href('Face', str(row['trace_id']))})"
                    if clip_href("Face", str(row["trace_id"])) != "-"
                    else "-",
                    row["recognition_detail"],
                    ", ".join(row["observed_stages"]),
                    ", ".join(row["missing_stages"]) or "-",
                ]
                for row in failed_face_trace_rows
            ],
        ),
    ]
    if not groups:
        lines.extend(["", "Không có failure trace."])
    else:
        lines.extend(["", "## Lifecycle traces", ""])
        evidence_root = output / "media"

        def stage_image(record: dict[str, Any], stage: str) -> Path | None:
            artifact_path = record.get("annotated_artifact_path") or record.get(
                "artifact_path"
            )
            if artifact_path:
                candidate = output / "media" / str(artifact_path)
                if candidate.is_file():
                    return candidate
            track_id = str(record.get("track_id") or "")
            if not track_id or not evidence_root.is_dir():
                return None
            pipeline_root = evidence_root / (
                "face"
                if record.get("pipeline") == "face" or record.get("task") == "face"
                else "lpr"
            )
            candidates = sorted(
                path
                for path in pipeline_root.rglob("*")
                if path.is_file()
                and stage in path.name
                and (
                    track_id in path.parent.name
                    or str(record.get("trace_id") or "") in str(path)
                )
            )
            return candidates[0] if candidates else None

        def stage_result(record: dict[str, Any] | None) -> str:
            if record is None:
                return "MISSING"
            stage = str(record.get("stage"))
            field_groups = {
                "detector_hit": (
                    "label",
                    "score",
                    "object_box",
                    "detection_region",
                    "source_pts",
                ),
                "track_seen": ("track_id", "generation", "object_box", "source_pts"),
                "lpr_eligible": (
                    "accepted",
                    "reason",
                    "position_changes",
                    "stationary",
                    "motionless_count",
                    "eligibility_retry",
                ),
                "plate_detector_input": (
                    "artifact_path",
                    "scale",
                    "detection_threshold",
                    "object_box",
                ),
                "plate_detector_result": (
                    "accepted",
                    "reason",
                    "score",
                    "box",
                    "plate_box",
                ),
                "plate_crop": (
                    "artifact_path",
                    "artifact_bytes",
                    "image_shape",
                    "plate_box",
                ),
                "ocr_plate_input": (
                    "artifact_path",
                    "artifact_bytes",
                    "image_shape",
                    "ocr_variant",
                ),
                "ocr_result": (
                    "accepted",
                    "reason",
                    "plate",
                    "normalized_plate",
                    "score",
                    "mean_character_score",
                    "character_scores",
                    "ocr_path",
                ),
                "ocr_text_crop": (
                    "artifact_path",
                    "artifact_bytes",
                    "image_shape",
                    "text_box",
                ),
                "ocr_recognition_tensor": (
                    "artifact_path",
                    "artifact_bytes",
                    "image_shape",
                    "ocr_variant",
                    "score",
                ),
                "event_published": (
                    "published",
                    "accepted",
                    "reason",
                    "event_id",
                    "plate",
                    "score",
                    "identity",
                ),
                "first_qualified_face": (
                    "admitted",
                    "reason",
                    "person_box",
                    "face_box",
                    "quality_score",
                    "quality_components",
                    "source_pts",
                ),
                "candidate_submitted": (
                    "candidate_id",
                    "frame_id",
                    "evidence_id",
                    "identity",
                    "quality_score",
                    "admitted",
                    "source_pts",
                ),
                "first_attempt": (
                    "attempt",
                    "task",
                    "identity",
                    "score",
                    "top1",
                    "top2",
                    "margin",
                    "latency_ms",
                    "status",
                    "reason",
                ),
                "confirmed_result": (
                    "identity",
                    "confidence",
                    "margin",
                    "status",
                    "reason",
                    "event_id",
                    "source_pts",
                ),
            }
            fields = field_groups.get(
                stage,
                (
                    "reason",
                    "decision",
                    "accepted",
                    "score",
                    "plate",
                    "identity",
                    "track_id",
                    "candidate_id",
                    "evidence_id",
                    "event_id",
                    "artifact_path",
                    "source_pts",
                ),
            )
            values = {key: record.get(key) for key in fields}
            values = {key: value for key, value in values.items() if value is not None}
            return json.dumps(values, ensure_ascii=False, separators=(",", ":")) or "{}"

        for (kind, trace_id), records in sorted(groups.items()):
            required_stages = (
                [
                    "track_seen",
                    "lpr_eligible",
                    "plate_detector_input",
                    "plate_detector_result",
                    "plate_crop",
                    "ocr_plate_input",
                    "ocr_result",
                    "ocr_text_crop",
                    "ocr_recognition_tensor",
                    "event_published",
                ]
                if kind == "LPR"
                else [
                    "detector_hit",
                    "track_seen",
                    "first_qualified_face",
                    "candidate_submitted",
                    "first_attempt",
                    "confirmed_result",
                ]
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
                        (record or {}).get(
                            "source_pts", (record or {}).get("frame_time")
                        ),
                        (record or {}).get("trace_time"),
                        "MISSING" if record is None else "observed",
                        stage_result(record),
                        image_link,
                    ]
                )
            lines.extend(
                [
                    f"### `{kind}` track_id `{trace_id}`",
                    "",
                    f"Clip: [{clip_href(kind, trace_id)}]({clip_href(kind, trace_id)})"
                    if clip_href(kind, trace_id) != "-"
                    else "Clip: -",
                    "",
                    _md_table(
                        [
                            "Stage",
                            "Source PTS",
                            "Runtime time",
                            "Status",
                            "Result",
                            "Image",
                        ],
                        lifecycle_rows,
                    ),
                    "",
                ]
            )
    diagnostic_failures = []
    if not runtime.get("lpr_evidence", {}).get("valid", True):
        evidence = runtime.get("lpr_evidence", {})
        diagnostic_failures.append(
            [
                "lpr_evidence",
                (
                    f"{evidence.get('reason')}; invocations={evidence.get('invocations')}; "
                    f"errors={len(evidence.get('errors', []))}"
                ),
            ]
        )
    native_media = runtime.get("native_media", {})
    if native_media and not native_media.get("complete", False):
        missing = [
            f"{item.get('trace_id')}={item.get('clip_reason')}"
            for item in native_media.get("traces", [])
            if item.get("clip_status") != "recorded"
        ]
        diagnostic_failures.append(["native_media", "; ".join(missing) or "incomplete"])
    if runtime.get("correlation_mismatches"):
        diagnostic_failures.append(
            ["correlation", len(runtime["correlation_mismatches"])]
        )
    if runtime.get("hardware_sampler_errors"):
        diagnostic_failures.append(
            ["hardware_sampler", "; ".join(runtime["hardware_sampler_errors"])]
        )
    if runtime.get("bad_log_lines"):
        diagnostic_failures.append(["runtime_logs", len(runtime["bad_log_lines"])])
    if not summary.get("measurement", {}).get("source_pts_complete", True):
        diagnostic_failures.append(["source_pts", "missing source PTS"])
    if diagnostic_failures:
        lines.extend(
            [
                "## Diagnostic failures",
                "",
                _md_table(["Area", "Detail"], diagnostic_failures),
                "",
            ]
        )
    report_path = output / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def write_runtime_image_index(
    output: Path,
    lpr_records: list[dict[str, Any]],
    face_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Index producer-owned images without creating or altering image evidence."""
    media_root = output / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    entries: dict[Path, dict[str, Any]] = {}
    decisions: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(
        list
    )

    def result_value(record: dict[str, Any]) -> str:
        parts = []
        for key in (
            "accepted",
            "reason",
            "plate",
            "normalized_plate",
            "raw_identity",
            "identity",
            "raw_score",
            "score",
        ):
            if record.get(key) is not None:
                parts.append(f"{key}={record[key]}")
        return "; ".join(parts) or "observed"

    def geometry_value(record: dict[str, Any]) -> str:
        parts = []
        for key in (
            "object_box",
            "plate_box",
            "detail_box",
            "effective_crop_box",
            "text_box",
        ):
            if record.get(key) is not None:
                parts.append(f"{key}={record[key]}")
        return "; ".join(parts) or "—"

    def add_image(
        path: Path,
        pipeline: str,
        trace_id: str,
        evidence_id: str,
        record: dict[str, Any] | None = None,
    ) -> None:
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(media_root.resolve())
            or not resolved.is_file()
            or resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
        ):
            return
        record = record or {}
        if resolved in entries and not record:
            return
        sequence_match = re.match(r"^(\d+)-", resolved.stem)
        stage = str(record.get("stage") or re.sub(r"^\d+-", "", resolved.stem))
        entries[resolved] = {
            "pipeline": pipeline,
            "trace_id": trace_id,
            "evidence_id": evidence_id,
            "stage": stage,
            "sequence": record.get("sequence")
            or (int(sequence_match.group(1)) if sequence_match else "—"),
            "source_pts": record.get("source_pts", record.get("frame_time", "—")),
            "result": result_value(record),
            "geometry": geometry_value(record),
            "sha256": record.get("artifact_sha256") or "—",
            "bytes": record.get("artifact_bytes") or resolved.stat().st_size,
        }

    for record in lpr_records:
        trace_id = str(record.get("trace_id") or record.get("track_id") or "unknown")
        evidence_id = str(record.get("evidence_id") or "unknown")
        if record.get("stage") in {
            "eligibility_decision",
            "plate_detector_result",
            "ocr_candidate_result",
            "ocr_result",
        }:
            decisions[("lpr", trace_id)].append(
                {
                    "sequence": record.get("sequence", "—"),
                    "source_pts": record.get(
                        "source_pts", record.get("frame_time", "—")
                    ),
                    "stage": record.get("stage", "unknown"),
                    "evidence_id": evidence_id,
                    "result": result_value(record),
                    "geometry": geometry_value(record),
                    "sha256": "—",
                    "bytes": "—",
                }
            )
        relative = record.get("artifact_path")
        if not relative:
            continue
        add_image(
            media_root / str(relative),
            "lpr",
            trace_id,
            evidence_id,
            record,
        )

    # Each annotated Face record identifies a producer-owned evidence directory.
    # Its raw attempt, bbox image and crop are sibling artifacts written by the
    # same model attempt; evidence.json prevents indexing unrelated files.
    face_evidence_dirs: dict[Path, tuple[str, str]] = {}
    for record in face_records:
        relative = record.get("artifact_path") or record.get("annotated_artifact_path")
        if not relative:
            continue
        add_image(
            media_root / str(relative),
            "face",
            str(record.get("trace_id") or record.get("track_id") or "unknown"),
            str(record.get("evidence_id") or "unknown"),
            record,
        )
        directory = (media_root / str(relative)).parent
        if not (directory / "evidence.json").is_file():
            continue
        face_evidence_dirs[directory] = (
            str(record.get("trace_id") or record.get("track_id") or "unknown"),
            str(record.get("evidence_id") or directory.name),
        )
    for directory, (trace_id, evidence_id) in sorted(face_evidence_dirs.items()):
        for path in sorted(directory.iterdir()):
            add_image(path, "face", trace_id, evidence_id)

    grouped: dict[tuple[str, str], list[tuple[Path, dict[str, Any]]]] = (
        collections.defaultdict(list)
    )
    stage_counts: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for path, metadata in sorted(entries.items()):
        grouped[(metadata["pipeline"], metadata["trace_id"])].append((path, metadata))
        stage_counts[metadata["pipeline"]][metadata["stage"]] += 1

    lines = [
        "# Producer-owned image evidence",
        "",
        "Gallery này chỉ lập chỉ mục artifact do pipeline ghi. Validator không vẽ, cắt, "
        "sao chép hoặc tạo ảnh thay thế.",
        "",
        _md_table(
            ["Pipeline", "Traces", "Images", "Stage counts"],
            [
                [
                    pipeline.upper(),
                    len(
                        {
                            trace
                            for item_pipeline, trace in grouped
                            if item_pipeline == pipeline
                        }
                    ),
                    sum(stage_counts[pipeline].values()),
                    "; ".join(
                        f"{stage}={count}"
                        for stage, count in sorted(stage_counts[pipeline].items())
                    )
                    or "none",
                ]
                for pipeline in ("lpr", "face")
            ],
        ),
    ]
    for pipeline in ("lpr", "face"):
        lines.extend(["", f"## {pipeline.upper()}"])
        pipeline_groups = [
            (key, value) for key, value in grouped.items() if key[0] == pipeline
        ]
        for (_, trace_id), images in sorted(pipeline_groups):
            anchor = "trace-" + re.sub(r"[^a-z0-9-]+", "-", trace_id.lower()).strip("-")
            lines.extend(
                [
                    "",
                    f'<a id="{anchor}"></a>',
                    "",
                    f"### `{trace_id}`",
                    "",
                    "<details>",
                    f"<summary>{len(images)} producer images · "
                    f"{len(decisions[(pipeline, trace_id)])} decision records</summary>",
                    "",
                ]
            )
            timeline_rows = []
            timeline = [
                *images,
                *[(None, item) for item in decisions[(pipeline, trace_id)]],
            ]
            for path, metadata in sorted(
                timeline,
                key=lambda item: (
                    int(item[1]["sequence"])
                    if str(item[1]["sequence"]).isdigit()
                    else 0
                ),
            ):
                image = "—"
                integrity = "—"
                if path is not None:
                    relative = path.relative_to(media_root).as_posix()
                    image = _md_thumbnail(relative, str(metadata["stage"]))
                    integrity = f"{metadata['bytes']} B<br>`{metadata['sha256']}`"
                timeline_rows.append(
                    [
                        metadata["sequence"],
                        metadata["source_pts"],
                        metadata["stage"],
                        metadata["evidence_id"],
                        metadata["result"],
                        metadata["geometry"],
                        image,
                        integrity,
                    ]
                )
            lines.append(
                _md_table(
                    [
                        "Seq",
                        "Source PTS",
                        "Stage",
                        "Evidence",
                        "Decision / result",
                        "BBox / crop",
                        "Producer image",
                        "Integrity",
                    ],
                    timeline_rows,
                )
            )
            lines.append("</details>")

    index_path = media_root / "images.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "path": index_path,
        "total": len(entries),
        "lpr": sum(stage_counts["lpr"].values()),
        "face": sum(stage_counts["face"].values()),
        "traces": {trace_id for _, trace_id in grouped},
    }


def write_compact_runtime_report(output: Path, summary: dict[str, Any]) -> Path:
    """Write one compact, non-duplicative runtime report from producer evidence."""
    runtime = summary.get("runtime", {})
    resources = runtime.get("resources", {})
    gpu = resources.get("gpu", {})
    timing = summary.get("timing", {})
    measurement = summary.get("measurement", {})

    def load_records(name: str) -> list[dict[str, Any]]:
        path = output / name
        if not path.is_file():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("records", [])

    def load_jsonl_records(relative: str) -> list[dict[str, Any]]:
        path = output / relative
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    runtime_records = load_records("runtime-trace.json")
    if not runtime_records:
        runtime_records = load_jsonl_records("media/runtime-trace.jsonl")
    lpr_evidence_records = load_records("runtime-evidence.json")
    if not lpr_evidence_records:
        lpr_evidence_records = load_jsonl_records("media/lpr/evidence.jsonl")
    face_evidence_records = load_jsonl_records("media/face/evidence.jsonl")
    face_annotated_records = load_records("face-annotated-evidence.json")
    records = [
        *runtime_records,
        *lpr_evidence_records,
        *(face_evidence_records or face_annotated_records),
    ]
    image_index = write_runtime_image_index(
        output,
        lpr_evidence_records,
        face_evidence_records or face_annotated_records,
    )

    def group_records(pipeline: str) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for record in records:
            if record.get("pipeline") != pipeline:
                continue
            trace_id = str(record.get("trace_id") or record.get("track_id") or "")
            if trace_id and not trace_id.startswith("detector:"):
                grouped[trace_id].append(record)
        return grouped

    lpr_by_trace = group_records("lpr")
    face_by_trace = group_records("face")

    def ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: float(
                item.get("source_pts", item.get("frame_time", 0)) or 0
            ),
        )

    def clip_href(pipeline: str, trace_id: str) -> str | None:
        safe_trace = re.sub(r"[^A-Za-z0-9_.-]+", "_", trace_id)
        relative = Path("media") / pipeline / safe_trace / "clip.mp4"
        return relative.as_posix() if (output / relative).is_file() else None

    def compact_mapping(value: Any) -> str:
        if not isinstance(value, dict):
            return str(value if value not in (None, "") else "none")
        return (
            "; ".join(f"{key}={item}" for key, item in sorted(value.items())) or "none"
        )

    expected_rows = summary.get("lpr", {}).get("passages", [])
    expected_by_plate = {
        normalize_plate(row.get("expected_plate")): str(row.get("passage_id"))
        for row in expected_rows
        if normalize_plate(row.get("expected_plate"))
    }
    lpr_runtime: list[dict[str, Any]] = []
    for index, (trace_id, trace_records) in enumerate(sorted(lpr_by_trace.items()), 1):
        published = [
            record
            for record in ordered(trace_records)
            if record.get("stage") == "event_published" and record.get("plate")
        ]
        final_plate = normalize_plate(published[-1].get("plate")) if published else ""
        fixture_match = expected_by_plate.get(final_plate)
        lpr_runtime.append(
            {
                "index": index,
                "trace_id": trace_id,
                "records": trace_records,
                "plate": final_plate,
                "fixture": fixture_match,
                "outcome": "MATCH"
                if fixture_match
                else ("UNEXPECTED" if final_plate else "NO_OUTPUT"),
            }
        )

    def lpr_clip_cell(row: dict[str, Any]) -> str:
        href = clip_href("lpr", str(row["trace_id"]))
        return f"[L{int(row['index']):02d}]({href})" if href else "—"

    def lpr_stage_note(row: dict[str, Any], *preferred_stages: str) -> str:
        stage_records: list[dict[str, Any]] = []
        for stage in preferred_stages:
            stage_records = [
                record
                for record in ordered(row["records"])
                if record.get("stage") == stage
            ]
            if stage_records:
                break
        if not stage_records:
            return "—"
        final = stage_records[-1]
        rejected = final.get("accepted") is False or final.get("status") in {
            "failed",
            "rejected",
            "error",
        }
        note = ["✗" if rejected else "✓"]
        if preferred_stages == ("track_seen",):
            note.append(f"×{len(stage_records)}")
        value = final.get("plate") or final.get("normalized_plate")
        if value:
            note.append(str(value))
        if final.get("score") is not None:
            note.append(f"{float(final['score']):.3f}")
        reason = final.get("reason")
        if reason:
            note.append(str(reason))
        return " · ".join(note)

    lpr_rows = [
        [
            lpr_clip_cell(row),
            row["outcome"],
            lpr_stage_note(row, "track_seen"),
            lpr_stage_note(row, "eligibility_decision", "lpr_eligible"),
            lpr_stage_note(row, "plate_detector_result", "plate_detected"),
            lpr_stage_note(row, "ocr_result"),
            lpr_stage_note(row, "event_published"),
        ]
        for row in lpr_runtime
    ]

    def face_clip_cell(trace_id: str, index: int) -> str:
        href = clip_href("face", trace_id)
        return f"[F{index:02d}]({href})" if href else "—"

    face_rows: list[list[Any]] = []
    face_runtime: list[dict[str, Any]] = []
    face_outcomes: list[str] = []
    for index, (trace_id, trace_records) in enumerate(sorted(face_by_trace.items()), 1):
        attempts = [
            record for record in trace_records if record.get("stage") == "first_attempt"
        ]
        confirmed = [
            record
            for record in trace_records
            if record.get("stage") == "confirmed_result"
        ]
        prepared = [
            record for record in trace_records if record.get("stage") == "face_crop"
        ]
        observations = sum(
            record.get("stage") == "track_seen" for record in trace_records
        )
        identities = sorted(
            {
                str(record.get("identity"))
                for record in attempts
                if record.get("identity")
            }
        )
        published = sorted(
            {
                str(record.get("identity"))
                for record in confirmed
                if record.get("identity")
            }
        )
        scores = [
            float(record["score"])
            for record in attempts
            if record.get("score") is not None
        ]
        outcome = face_trace_outcome(trace_records)
        face_outcomes.append(outcome)
        identity_text = ", ".join(identities) or "none"
        if scores:
            identity_text += f" ({min(scores):.3f}..{max(scores):.3f})"
        attempt_note = f"✓ · ×{len(attempts)} · {identity_text}" if attempts else "—"
        vote_note = (
            f"✓ · {', '.join(published)}"
            if confirmed
            else ("no aggregate · unknown excluded" if attempts else "—")
        )
        confirmed_note = (
            f"✓ · ×{len(confirmed)} · {', '.join(published)}" if confirmed else "—"
        )
        face_rows.append(
            [
                face_clip_cell(trace_id, index),
                outcome,
                (
                    f"×{len(prepared)} crops / {observations} input frames"
                    if prepared
                    else f"— / {observations} input frames"
                ),
                attempt_note,
                f"{vote_note}; publish {confirmed_note}",
            ]
        )
        face_runtime.append(
            {
                "index": index,
                "trace_id": trace_id,
                "records": trace_records,
                "outcome": outcome,
            }
        )

    lpr_match = sum(row["outcome"] == "MATCH" for row in lpr_runtime)
    lpr_unexpected = sum(row["outcome"] == "UNEXPECTED" for row in lpr_runtime)
    lpr_no_output = sum(row["outcome"] == "NO_OUTPUT" for row in lpr_runtime)
    lpr_missing = max(0, len(expected_rows) - lpr_match)
    face_known = sum(value == "recognized_known_published" for value in face_outcomes)
    face_unknown = sum(value == "recognized_unknown" for value in face_outcomes)
    face_failed = sum(value == "not_recognized" for value in face_outcomes)
    recognition = runtime.get("recognition", {})
    native_media = runtime.get("native_media", {})
    recognition_service = runtime.get("recognition_service", {})
    cleanup = (
        "zero"
        if recognition.get("cleanup_zero")
        else (
            f"sessions={recognition.get('sessions', '—')}; "
            f"flight={recognition.get('in_flight', '—')}; "
            f"leases={recognition.get('evidence_pinned', '—')}; "
            f"queue={recognition.get('queue_depth', '—')}"
        )
    )

    hardware_samples = runtime.get("hardware_samples", {})
    cpu_samples = [float(value) for value in hardware_samples.get("cpu_percent", [])]
    ram_samples = [float(value) for value in hardware_samples.get("ram_bytes", [])]
    shm_samples = [float(value) for value in hardware_samples.get("shm_percent", [])]
    gpu_samples = hardware_samples.get("gpu", [])

    def average(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    def gib(value: float | int | None) -> str:
        return f"{float(value) / (1024**3):.2f} GiB" if value is not None else "—"

    gpu_utilization = [
        float(sample["utilization_percent"])
        for sample in gpu_samples
        if sample.get("utilization_percent") is not None
    ]
    gpu_memory = [
        float(sample["memory_used_mib"])
        for sample in gpu_samples
        if sample.get("memory_used_mib") is not None
    ]

    def lifecycle_result(record: dict[str, Any] | None) -> str:
        if record is None:
            return "MISSING"
        values = {
            key: record.get(key)
            for key in (
                "accepted",
                "reason",
                "status",
                "attempt",
                "plate",
                "normalized_plate",
                "score",
                "identity",
                "raw_identity",
                "raw_score",
                "top1",
                "top2",
                "margin",
            )
            if record.get(key) is not None
        }
        return (
            json.dumps(values, ensure_ascii=False, separators=(",", ":")) or "observed"
        )

    def lifecycle_image(trace_records: list[dict[str, Any]], stage: str) -> str:
        image_stage_aliases = {
            "track_seen": ("runtime_frame_object_box", "recognition_attempt_bbox"),
            "lpr_eligible": ("car_crop", "runtime_frame_object_box"),
            "plate_detector_result": ("plate_detector_input",),
            "ocr_result": ("ocr_plate_input",),
            "event_published": ("ocr_recognition_tensor", "ocr_text_crop"),
            "first_qualified_face": ("recognition_attempt_bbox",),
            "candidate_submitted": ("recognition_attempt",),
            "first_attempt": ("recognition_attempt_bbox", "recognition_attempt"),
            "confirmed_result": ("recognition_attempt_bbox",),
        }
        wanted = (stage, *image_stage_aliases.get(stage, ()))
        selected: dict[str, Any] | None = None
        for wanted_stage in wanted:
            candidates = [
                record
                for record in ordered(trace_records)
                if record.get("stage") == wanted_stage and record.get("artifact_path")
            ]
            if candidates:
                selected = candidates[-1]
                break
        if selected is None:
            return "—"
        relative = str(selected["artifact_path"])
        report_relative = f"media/{Path(relative).as_posix()}"
        return (
            _md_thumbnail(report_relative, stage)
            if (output / "media" / relative).is_file()
            else "—"
        )

    lpr_lifecycle_lines: list[str] = []
    failed_runtime_lpr = [row for row in lpr_runtime if row["outcome"] != "MATCH"]
    if failed_runtime_lpr:
        lifecycle_stages = (
            "track_seen",
            "lpr_eligible",
            "plate_detector_input",
            "plate_detector_result",
            "plate_crop",
            "ocr_plate_input",
            "ocr_result",
            "ocr_text_crop",
            "ocr_recognition_tensor",
            "event_published",
        )
        lpr_lifecycle_lines.extend(["", "### Lifecycle traces", ""])
        for row in failed_runtime_lpr:
            by_stage: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
            for record in ordered(row["records"]):
                by_stage[str(record.get("stage"))].append(record)
            by_stage["lpr_eligible"].extend(by_stage.get("eligibility_decision", []))
            lifecycle_rows = []
            for stage in lifecycle_stages:
                stage_records = by_stage.get(stage, [])
                final_record = stage_records[-1] if stage_records else None
                lifecycle_rows.append(
                    [
                        stage,
                        len(stage_records),
                        (final_record or {}).get(
                            "source_pts", (final_record or {}).get("frame_time", "—")
                        ),
                        "MISSING" if final_record is None else "observed",
                        lifecycle_result(final_record),
                        lifecycle_image(row["records"], stage)
                        if final_record is not None
                        else "—",
                    ]
                )
            lpr_lifecycle_lines.extend(
                [
                    f"#### `{row['trace_id']}` — `{row['outcome']}`",
                    "",
                    _md_table(
                        [
                            "Stage",
                            "Records",
                            "Source PTS",
                            "Status",
                            "Final result",
                            "Image",
                        ],
                        lifecycle_rows,
                    ),
                    "",
                ]
            )

    face_lifecycle_lines: list[str] = []
    review_face_traces = [
        row
        for row in face_runtime
        if row["outcome"] in {"recognized_unknown", "not_recognized"}
    ]
    face_rendered_image_count = 0
    if review_face_traces:
        face_lifecycle_lines.extend(["", "### Lifecycle traces", ""])
        for row in review_face_traces:
            by_stage: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
            for record in ordered(row["records"]):
                by_stage[str(record.get("stage"))].append(record)

            stage_specs = (
                ("prepare_face_attempt", "face_crop", "recognition_attempt_bbox"),
                ("recognizer.classify", "first_attempt", "face_crop"),
                ("FaceEngine vote / publish", "first_attempt", "recognition_attempt"),
            )
            lifecycle_rows = []
            for production_stage, record_stage, image_stage in stage_specs:
                stage_records = by_stage.get(record_stage, [])
                final_record = stage_records[-1] if stage_records else None
                image_records = by_stage.get(image_stage, [])
                image = "—"
                if image_records:
                    relative = image_records[-1].get("artifact_path")
                    if relative and (output / "media" / str(relative)).is_file():
                        image = _md_thumbnail(
                            f"media/{Path(str(relative)).as_posix()}", production_stage
                        )
                        face_rendered_image_count += 1

                final_result = lifecycle_result(final_record)
                if production_stage == "FaceEngine vote / publish" and final_record:
                    identity = final_record.get("identity")
                    final_result = (
                        "no aggregate; unknown excluded; sub_label not published"
                        if identity == "unknown"
                        else f"{final_result}; weighted vote evaluated"
                    )
                lifecycle_rows.append(
                    [
                        production_stage,
                        len(stage_records),
                        (final_record or {}).get(
                            "source_pts", (final_record or {}).get("frame_time", "—")
                        ),
                        "MISSING" if final_record is None else "observed",
                        final_result,
                        image,
                    ]
                )
            face_lifecycle_lines.extend(
                [
                    f"#### `{row['trace_id']}` — `{row['outcome']}`",
                    "",
                    _md_table(
                        [
                            "Stage",
                            "Records",
                            "Source PTS",
                            "Status",
                            "Final result",
                            "Image",
                        ],
                        lifecycle_rows,
                    ),
                    "",
                ]
            )

    lines = [
        "# Runtime Test Report",
        "",
        *(
            [
                "> **Incomplete run:** Partial producer evidence recovered before "
                f"failure: `{summary['error']}`",
                "",
            ]
            if summary.get("error")
            else []
        ),
        "## Run",
        "",
        _md_table(
            [
                "Run",
                "Topology",
                "Report",
                "Measurement",
                "Service",
                "Epoch",
                "Local models",
                "Source PTS",
            ],
            [
                [
                    output.name,
                    summary.get("topology", "—"),
                    summary.get("report", {}).get("status", "—"),
                    measurement.get("measurement_valid", "—"),
                    "in-process"
                    if summary.get("topology") == "local"
                    else recognition_service.get("healthy", "—"),
                    recognition_service.get("service_epoch") or "n/a",
                    "enabled" if summary.get("topology") == "local" else "disabled",
                    measurement.get("source_pts_complete", "—"),
                ]
            ],
        ),
        "",
        "## LPR result",
        "",
        _md_table(
            [
                "Match",
                "Unexpected",
                "No output",
                "Expected missing",
                "Raw traces",
                "Clips",
                "Debug images",
            ],
            [
                [
                    f"{lpr_match}/{len(expected_rows)}",
                    lpr_unexpected,
                    lpr_no_output,
                    lpr_missing,
                    len(lpr_runtime),
                    sum(
                        clip_href("lpr", row["trace_id"]) is not None
                        for row in lpr_runtime
                    ),
                    f"[{image_index['lpr']} LPR images](media/images.md#lpr)",
                ]
            ],
        ),
        "",
        "Bảng dưới đây chỉ dùng producer trace. Fixture chỉ tham gia tính KPI Match ở bảng tổng hợp.",
        "",
        _md_table(
            ["Clip", "Outcome", "Track", "Eligible", "Plate", "OCR", "Publish"],
            lpr_rows,
        ),
        *lpr_lifecycle_lines,
        "",
        "## Face result",
        "",
        _md_table(
            [
                "Known",
                "Unknown",
                "Failed",
                "Raw traces",
                "Attempts",
                "Published",
                "Clips",
                "Rendered images",
                "Artifact gallery",
            ],
            [
                [
                    face_known,
                    face_unknown,
                    face_failed,
                    len(face_rows),
                    sum(
                        record.get("stage") == "first_attempt"
                        for records_for_trace in face_by_trace.values()
                        for record in records_for_trace
                    ),
                    summary.get("face", {}).get("recognition_publish_count", 0),
                    sum(
                        clip_href("face", trace_id) is not None
                        for trace_id in face_by_trace
                    ),
                    face_rendered_image_count,
                    f"[{image_index['face']} artifacts](media/images.md#face)",
                ]
            ],
        ),
        "",
        "Face chỉ có ba stage nghiệp vụ production; input frame và producer evidence chỉ dùng để truy vết.",
        "",
        _md_table(
            [
                "Clip",
                "Outcome",
                "Prepare face",
                "Recognition",
                "Decision / publish",
            ],
            face_rows,
        ),
        *face_lifecycle_lines,
        "",
        "## Hardware and runtime health",
        "",
        _md_table(
            [
                "Samples",
                "RAM avg / peak",
                "CPU avg / peak",
                "GPU avg / peak",
                "VRAM avg / peak / total",
                "SHM avg / peak",
                "Skipped FPS",
            ],
            [
                [
                    max(
                        len(cpu_samples),
                        len(ram_samples),
                        len(shm_samples),
                        len(gpu_samples),
                    ),
                    f"{gib(average(ram_samples))} / {gib(max(ram_samples) if ram_samples else None)}",
                    f"{average(cpu_samples) if cpu_samples else '—'}% / "
                    f"{max(cpu_samples) if cpu_samples else '—'}% aggregate",
                    f"{average(gpu_utilization) if gpu_utilization else '—'}% / "
                    f"{max(gpu_utilization) if gpu_utilization else '—'}%",
                    f"{average(gpu_memory) if gpu_memory else '—'} / "
                    f"{max(gpu_memory) if gpu_memory else '—'} / "
                    f"{gpu.get('memory_total_mib', '—')} MiB",
                    f"{average(shm_samples) if shm_samples else '—'}% / "
                    f"{max(shm_samples) if shm_samples else '—'}%",
                    compact_mapping(resources.get("skipped_fps_max")),
                ]
            ],
        ),
        "",
        _md_table(
            [
                "Duration total / replay / restore",
                "Sessions",
                "In-flight",
                "Pinned leases",
                "Queue / outcomes",
                "Rejected",
                "Writer depth / drop / error",
                "Cleanup",
                "Native clips",
                "Sampler errors",
            ],
            [
                [
                    f"{timing.get('total_seconds', '—')} / "
                    f"{timing.get('replay_seconds', '—')} / "
                    f"{timing.get('restore_seconds', '—')} s",
                    recognition.get("sessions", "—"),
                    recognition.get("in_flight", "—"),
                    recognition.get("evidence_pinned", "—"),
                    f"{recognition.get('queue_depth', '—')} / "
                    f"{recognition.get('outcome_depth', '—')}",
                    recognition.get("rejected", 0),
                    f"{recognition.get('writer_depth', '—')} / "
                    f"{recognition.get('writer_drops', '—')} / "
                    f"{recognition.get('writer_errors', '—')}",
                    cleanup,
                    f"{native_media.get('recorded_count', 0)} / "
                    f"{native_media.get('trace_count', 0)}",
                    "; ".join(runtime.get("hardware_sampler_errors", [])) or "none",
                ]
            ],
        ),
        "",
        "CPU là tổng trên nhiều logical cores; cột hiển thị average/peak của toàn cửa sổ đo.",
    ]

    report_path = output / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one Platform runtime evidence replay and create a timestamped report."
    )
    parser.add_argument(
        "--topology", choices=("local", "recognition", "tracker"), default="local"
    )
    parser.add_argument(
        "--fault-scenario",
        choices=(
            "service_restart",
            "tracker_restart",
            "stream_disconnect",
            "client_disconnect",
            "spool_replay",
            "media_unavailable",
        ),
        help="Inject one launcher-owned fault during replay.",
    )
    args = parser.parse_args(argv)
    topology = str(args.topology)
    if args.fault_scenario and topology == "local":
        parser.error("--fault-scenario requires a recognition or tracker topology")
    if topology == "tracker" and args.fault_scenario == "service_restart":
        parser.error("tracker topology uses --fault-scenario tracker_restart")
    if topology == "recognition" and args.fault_scenario not in (
        None,
        "service_restart",
        "stream_disconnect",
        "client_disconnect",
    ):
        parser.error("tracker fault scenario requires --topology tracker")
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
        "topology": topology,
        "fault_scenario": args.fault_scenario,
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
    previous_capture_start = os.environ.get("PASSAGE_CAPTURE_START_PATH")
    previous_capture_cutoff = os.environ.get("PASSAGE_CAPTURE_CUTOFF_PATH")
    previous_run_id = os.environ.get("PASSAGE_RUN_ID")
    previous_source_start_dir = os.environ.get("PASSAGE_SOURCE_START_DIR")
    previous_evidence_bytes = os.environ.get("PASSAGE_EVIDENCE_MAX_BYTES")
    previous_evidence_records = os.environ.get("PASSAGE_EVIDENCE_MAX_RECORDS")
    previous_ready_seconds = os.environ.get("CAMERA_READY_STABLE_SECONDS")
    previous_skip_ready = os.environ.get("CAMERA_SKIP_READY_WAIT")
    previous_source_overlay = os.environ.get("CAMERA_SOURCE_OVERLAY")
    previous_report_media = os.environ.get("CAMERA_REPORT_MEDIA_DIR")
    previous_recognition_tls = os.environ.get("RECOGNITION_TLS_DIR")
    runtime_started = False
    sampler: ResourceSampler | None = None
    fault_injector: DockerFaultInjector | LauncherFaultInjector | None = None
    isolated_start_wall: float | None = None
    exit_code = 1
    runtime_workspace = output / "test-assets"
    runtime_workspace.mkdir(parents=True, exist_ok=True)
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
            "--workspace",
            str(runtime_workspace),
        ]
        base_config = yaml.safe_load(config.read_text(encoding="utf-8"))
        # Keep every generated fixture asset inside this timestamped run.
        cache_valid = False
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
        tls_directory = configure_recognition_topology(
            value, topology, runtime_workspace
        )
        tracker_tls_directory = configure_tracker_topology(value, topology)
        if tls_directory is not None:
            os.environ["RECOGNITION_TLS_DIR"] = str(tls_directory.resolve())
        if tracker_tls_directory is not None:
            summary["tracker_tls_directory"] = str(tracker_tls_directory.resolve())
        isolated_config.write_text(
            yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        fixture["config_sha256"] = sha256(isolated_config)
        write_json(output / "fixture.json", fixture)
        summary["fixture"] = fixture
        summary["fixture_contract"] = contract
        summary["timing"]["fixture_seconds"] = round(time.monotonic() - step_started, 3)
        # All topology mutations, including the pre-acceptance stop, are launcher-owned.
        runtime_started = True
        run_deploy(
            "stop",
            config,
            timeout=90 if topology in ("recognition", "tracker") else 30,
        )

        isolated_start_wall = time.time()
        step_started = time.monotonic()
        os.environ["PASSAGE_TRACE_PATH"] = TRACE_CONTAINER_PATH
        os.environ["PASSAGE_EVIDENCE_DIR"] = EVIDENCE_CONTAINER_DIR
        os.environ["PASSAGE_CAPTURE_START_PATH"] = CAPTURE_START_CONTAINER_PATH
        os.environ["PASSAGE_CAPTURE_CUTOFF_PATH"] = CAPTURE_CUTOFF_CONTAINER_PATH
        os.environ["PASSAGE_RUN_ID"] = run_id
        if topology == "local":
            os.environ["CAMERA_SOURCE_OVERLAY"] = str(
                (Path.cwd() / "frigate" / "frigate").resolve()
            )
        else:
            os.environ.pop("CAMERA_SOURCE_OVERLAY", None)
        report_media = output / "media"
        report_media.mkdir(parents=True, exist_ok=True)
        source_start_dir = report_media / "source-start"
        source_start_dir.mkdir(parents=True, exist_ok=True)
        os.environ["CAMERA_REPORT_MEDIA_DIR"] = str(report_media.resolve())
        os.environ["PASSAGE_SOURCE_START_DIR"] = SOURCE_START_CONTAINER_DIR
        os.environ["PASSAGE_EVIDENCE_MAX_BYTES"] = str(128 * 1024**2)
        os.environ["PASSAGE_EVIDENCE_MAX_RECORDS"] = "4096"
        os.environ["CAMERA_READY_STABLE_SECONDS"] = "2"
        os.environ["CAMERA_SKIP_READY_WAIT"] = "1"
        capture_start = time.time()
        (report_media / "capture-start").write_text(
            f"{capture_start:.9f}\n", encoding="utf-8"
        )
        summary["capture_start_epoch"] = capture_start
        try:
            run_deploy(
                "acceptance-start",
                Path(fixture["config"]),
                timeout=180 if topology in ("recognition", "tracker") else 30,
            )
            launcher_state_path = Path(".tmp/runtime/state.json")
            if not launcher_state_path.is_file():
                raise RuntimeError("launcher state artifact is missing")
            launcher_state = json.loads(
                launcher_state_path.read_text(encoding="utf-8")
            )
            write_json(output / "launcher-state.json", launcher_state)
            summary["launcher_state"] = launcher_state
        finally:
            if previous_source_overlay is None:
                os.environ.pop("CAMERA_SOURCE_OVERLAY", None)
            else:
                os.environ["CAMERA_SOURCE_OVERLAY"] = previous_source_overlay
        source_started = wait_source_starts(source_start_dir, timeout=60)
        if topology == "tracker":
            tracker_state = wait_tracker_ready(
                Path(".tmp/runtime/state.json"),
                set(CAMERAS.values()),
                require_cameras=False,
            )
            summary["tracker_readiness"] = tracker_state.get("tracker_nodes", [])
        wait_acceptance_ready(
            str(value["runtime"]["image"]), timeout=60, topology=topology
        )
        summary["timing"]["isolated_start_seconds"] = round(
            time.monotonic() - step_started, 3
        )
        observation_wall = time.time()
        initial_restarts = restart_counts()
        sampler = ResourceSampler(topology)
        sampler.start()
        if args.fault_scenario:
            fault_injector = LauncherFaultInjector(
                args.fault_scenario, output, Path(fixture["config"])
            )
            fault_injector.start()

        replay_sources = value["runtime"]["direct"]["sources"]
        replays = {
            "face": Path(replay_sources["face_camera"]),
            "lpr": Path(replay_sources["car_camera"]),
        }
        # Direct sources run at their native wall-clock duration. Derive the
        # EOF deadline from the authoritative files so a long, unmodified
        # source is never cut merely to satisfy the old composite-fixture
        # timeout. The bounded margin covers startup and FIFO backpressure.
        source_deadline = (
            time.monotonic()
            + max(replay_duration(path) for path in replays.values())
            + 45.0
        )
        step_started = time.monotonic()
        anchors, anchor_details = observe_round_anchors(
            replays, source_started, source_deadline
        )
        source_ended = wait_source_ends(source_start_dir, source_deadline)
        wait_latest_through(source_ended, source_deadline)
        live_trace_path = report_media / "runtime-trace.jsonl"
        live_records = [
            json.loads(line)
            for line in live_trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        finalized_track_count = finalize_finite_source_tracks(live_records)
        capture_cutoff = time.time()
        summary["capture_cutoff_epoch"] = capture_cutoff
        (report_media / "capture-cutoff").write_text(
            f"{capture_cutoff:.9f}\n", encoding="utf-8"
        )
        source_done = source_ended
        recognition_idle = wait_recognition_idle()
        recordings_ready, recordings_through = wait_recordings_through(
            str(value["database"]["path"]),
            list(CAMERAS.values()),
            capture_cutoff + 0.5,
        )
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
        recognition_stats = sampler.recognition[-1] if sampler.recognition else {}
        final_restarts = restart_counts()

        # Frigate owns persisted evidence in both topologies. The external
        # service transports source artifacts but never writes this volume.
        evidence_pipelines = ("lpr", "face")
        for pipeline in evidence_pipelines:
            evidence_manifest = output / "media" / pipeline / "evidence.jsonl"
            if not wait_file_quiescent(evidence_manifest):
                raise RuntimeError(
                    f"Runtime {pipeline.upper()} evidence writer did not become "
                    "quiescent after replay"
                )

        trace_path = output / "media" / "runtime-trace.jsonl"
        if not trace_path.is_file():
            raise RuntimeError("Passage runtime trace is missing")
        records = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        foreign_run_ids = sorted(
            {
                str(record.get("run_id"))
                for record in records
                if record.get("run_id") != run_id
            }
        )
        if foreign_run_ids:
            raise RuntimeError(
                f"Runtime trace contains records from other runs: {foreign_run_ids}"
            )
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
        raw_lpr_trace_ids = pipeline_trace_ids(runtime_records, "lpr")
        raw_face_trace_ids = pipeline_trace_ids(runtime_records, "face")
        all_media_evidence = media_evidence_records(output / "media")
        recognition_evidence = (
            validate_recognition_evidence(
                output / "media", runtime_records, all_media_evidence
            )
            if topology in ("recognition", "tracker")
            else {"valid": True, "errors": []}
        )
        if not recognition_evidence["valid"]:
            raise RuntimeError(
                "Recognition evidence is invalid: "
                + "; ".join(recognition_evidence.get("errors", ()))
            )
        write_json(output / "recognition-evidence.json", recognition_evidence)
        face_annotated_evidence = annotate_face_evidence(
            output / "media", all_media_evidence
        )
        write_json(output / "face-annotated-evidence.json", face_annotated_evidence)
        native_media = collect_native_trace_clips(
            output,
            runtime_records + all_media_evidence,
            str(value["database"]["path"]),
            edge_owned=topology == "tracker",
        )
        tracker_lifecycle = (
            tracker_lifecycle_audit(str(value["database"]["path"]))
            if topology == "tracker"
            else {"valid": True, "journal_entries": 0, "event_count": 0}
        )
        write_json(output / "tracker-lifecycle.json", tracker_lifecycle)
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
            output / "media",
            anchors,
            durations,
            passages_by_camera,
        )
        if not runtime_lpr_evidence["valid"]:
            raise RuntimeError(
                "Runtime LPR evidence is invalid: "
                + "; ".join(runtime_lpr_evidence.get("errors", ()))
            )
        observed_lpr_trace_metrics = trace_metrics(evidence_records, replay_seconds)
        write_json(
            output / "runtime-evidence.json",
            {"summary": runtime_lpr_evidence, "records": evidence_records},
        )
        recognition = {
            **recognition_stats,
            "cleanup_zero": all(
                int(recognition_stats.get(field, 0)) == 0
                for field in (
                    "sessions",
                    "in_flight",
                    "evidence_pinned",
                    "queue_depth",
                    "outcome_depth",
                    "writer_depth",
                )
            ),
        }
        write_json(
            output / "runtime-trace.json",
            {"anchors": anchors, "records": runtime_records},
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
        lpr["raw_trace_count"] = len(raw_lpr_trace_ids)
        lpr["expected_trace_count"] = len(lpr_passages)
        correlation = correlation_mismatches(comparison_records)
        runtime_logs = docker_logs("frigate", observation_wall)
        model_logs = docker_logs("frigate", isolated_start_wall)
        tracker_logs = (
            docker_logs("camera-tracker-edge-local", isolated_start_wall)
            if topology == "tracker"
            else ""
        )
        if tracker_logs:
            model_logs = f"{model_logs}\n{tracker_logs}"
        recognition_logs = (
            docker_logs("camera-recognition", isolated_start_wall)
            if topology in ("recognition", "tracker")
            else ""
        )
        pending = parse_pending(runtime_logs)
        pending_source = "recognition_log"
        if (
            int(recognition_stats.get("in_flight", 0)) == 0
            and int(recognition_stats.get("sessions", 0)) == 0
            and evidence_pinned_final == 0
        ):
            pending = 0
            pending_source = "recognition_cleanup"
        bad_log_lines = [
            line
            for line in runtime_logs.splitlines()
            if re.search(
                r"reconnect|stall|no frames|ffmpeg.*(?:error|crash)", line, re.I
            )
        ]
        if topology in ("recognition", "tracker"):
            bad_log_lines.extend(
                line
                for line in recognition_logs.splitlines()
                if re.search(r"traceback|service_disconnected|epoch_mismatch", line, re.I)
            )
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
        service_epochs = re.findall(
            r"Recognition service listening .* epoch=([0-9a-f]+)", recognition_logs
        )
        recognition_service_started = topology == "local" or len(service_epochs) == 1
        local_models_disabled = topology == "local" or face_model_loads == 0
        with urlopen("http://127.0.0.1:5001/api/stats", timeout=2) as response:
            final_main_stats = json.loads(response.read().decode("utf-8"))
        if topology == "tracker":
            run_deploy("status", Path(fixture["config"]), timeout=30)
            launcher_state = json.loads(
                Path(".tmp/runtime/state.json").read_text(encoding="utf-8")
            )
            write_json(output / "launcher-state.json", launcher_state)
            summary["launcher_state"] = launcher_state
        main_camera_stats = final_main_stats.get("cameras", {})
        main_edge_ownership_zero = topology != "tracker" or all(
            not (main_camera_stats.get(camera) or {}).get("capture_pid")
            and not (main_camera_stats.get(camera) or {}).get("pid")
            for camera in CAMERAS.values()
        ) and not final_main_stats.get("detectors")
        launcher_tracker_nodes = summary.get("launcher_state", {}).get(
            "tracker_nodes", []
        )
        tracker_cameras_ready = topology != "tracker" or (
            len(launcher_tracker_nodes) == 1
            and {
                item.get("camera_id")
                for item in launcher_tracker_nodes[0].get("cameras", [])
                if item.get("ready")
            }
            == set(CAMERAS.values())
        )
        tracker_terminal_zero = topology != "tracker" or all(
            node.get("health", {}).get("pending_ack") == 0
            and node.get("health", {}).get("pinned_evidence") == 0
            and node.get("health", {}).get("active_lifecycles") == 0
            for node in launcher_tracker_nodes
        )
        tracker_not_degraded = topology != "tracker" or all(
            not node.get("health", {}).get("degraded")
            for node in launcher_tracker_nodes
        )

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
            "direct_sources": {
                "run_id": run_id,
                "started": source_started,
                "done": source_done,
                "explicit_track_ends": finalized_track_count,
            },
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
            "recognition_service": {
                "topology": topology,
                "started": recognition_service_started,
                "service_epoch": service_epochs[0] if len(service_epochs) == 1 else None,
                "local_models_disabled": local_models_disabled,
                "healthy": bool(recognition_stats.get("service_healthy", 0))
                if topology in ("recognition", "tracker")
                else None,
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
            "recognition_metrics": {"samples": sampler.recognition},
            "lpr_evidence": runtime_lpr_evidence,
            "recognition_evidence": recognition_evidence,
            "face_annotated_evidence": {
                key: value
                for key, value in face_annotated_evidence.items()
                if key != "records"
            },
            "recognition": recognition,
            "recognition_idle_after_replay": recognition_idle,
            "recordings_ready": recordings_ready,
            "recordings_available_through": recordings_through,
            "native_media": native_media,
            "tracker": {
                "nodes": launcher_tracker_nodes,
                "main_edge_ownership_zero": main_edge_ownership_zero,
                "cameras_ready": tracker_cameras_ready,
                "terminal_zero": tracker_terminal_zero,
                "not_degraded": tracker_not_degraded,
                "lifecycle": tracker_lifecycle,
            },
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
                    "face_library": fixture.get("face_library_snapshot", {}).get(
                        "sha256"
                    ),
                    "master_recognition_commit": MASTER_RECOGNITION_COMMIT,
                    "recognition_core": sha256_tree(
                        Path(__file__).resolve().parents[2]
                        / "frigate"
                        / "frigate"
                        / "recognition"
                    ),
                },
            }
        )
        write_json(output / "face.json", {**face, "latency": latency["face"]})
        write_json(output / "lpr.json", lpr)

        # Runtime evidence under media/<pipeline>/<trace_id> is authoritative.
        # Do not create a duplicate ground-truth mismatch folder.
        evidence_ok = True

        gates = {
            "fixture_contract": contract["valid"],
            "anchor_complete": runtime["rounds_complete"],
            "lpr_recall": lpr["passage_recall"] == 1.0,
            "lpr_readable_denominator": lpr["readable_denominator"] >= 3,
            "lpr_exact_match_reported": lpr["exact_match"] is not None,
            "lpr_accuracy": lpr["accuracy"] is not None and lpr["accuracy"] >= 2 / 3,
            "lpr_recognition_precision": lpr["precision"] == 1.0,
            "lpr_recognition_recall": lpr["recall"] >= 2 / 3,
            "lpr_passage_precision": lpr["passage_precision"] == 1.0,
            "lpr_raw_trace_count_exact": len(raw_lpr_trace_ids) == len(lpr_passages),
            "face_raw_trace_count_exact": len(raw_face_trace_ids)
            == len(face_passages),
            "face_raw_tracks_present": face.get("trace_count", 0) > 0,
            "face_raw_attempts_present": face.get("attempt_count", 0) > 0,
            "face_recognition_trace_coverage_complete": face.get(
                "recognition_coverage", 0.0
            )
            == 1.0,
            "face_library_snapshot": bool(
                fixture.get("face_library_snapshot", {}).get("identity_count", 0)
            )
            and bool(fixture.get("face_library_snapshot", {}).get("image_count", 0))
            and not fixture.get("face_library_snapshot", {}).get("train_copied", True),
            "face_annotated_evidence": face_annotated_evidence["valid"],
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
            and (
                face_model_loads == 1
                if topology == "local"
                else recognition_service_started and local_models_disabled
            ),
            "recognition_service_started": recognition_service_started,
            "external_has_no_local_models": local_models_disabled,
            "recognition_service_healthy": topology == "local"
            or bool(recognition_stats.get("service_healthy", 0)),
            "recognition_cleanup_zero": recognition["cleanup_zero"],
            "recognition_writer_drops_zero": recognition.get("writer_drops", 0) == 0,
            "recognition_writer_errors_zero": recognition.get("writer_errors", 0) == 0,
            "tracker_cameras_ready": tracker_cameras_ready,
            "main_edge_ownership_zero": main_edge_ownership_zero,
            "tracker_terminal_zero": tracker_terminal_zero,
            "tracker_not_degraded": tracker_not_degraded,
            "tracker_media_proxy_complete": topology != "tracker"
            or native_media.get("media_proxy_complete", False),
            "tracker_lifecycle_valid": tracker_lifecycle.get("valid", False),
        }
        summary["gates"] = gates
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary.setdefault("gates", {})["error_free"] = False
    finally:
        try:
            capture_container_diagnostics(output, isolated_start_wall, topology)
        except Exception as exc:
            summary.setdefault("diagnostic_errors", []).append(
                f"{type(exc).__name__}: {exc}"
            )
        if sampler is not None:
            sampler.stop()
        if fault_injector is not None:
            try:
                fault_injector.stop()
                summary.setdefault("gates", {})["fault_completed"] = (
                    output / "fault-record.json"
                ).is_file()
            except Exception as exc:
                summary.setdefault("fault_errors", []).append(
                    f"{type(exc).__name__}: {exc}"
                )
                summary.setdefault("gates", {})["fault_completed"] = False
        step_started = time.monotonic()
        restore_ok = not runtime_started
        if runtime_started:
            try:
                if previous_recognition_tls is None:
                    os.environ.pop("RECOGNITION_TLS_DIR", None)
                else:
                    os.environ["RECOGNITION_TLS_DIR"] = previous_recognition_tls
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
        if topology == "tracker":
            diagnostic_names = {
                "lpr_readable_denominator",
                "lpr_exact_match_reported",
                "lpr_accuracy",
                "lpr_recognition_precision",
                "lpr_recognition_recall",
            }
            required_gates = {
                name: passed
                for name, passed in summary["gates"].items()
                if name not in diagnostic_names
            }
            failed_gates = sorted(
                name for name, passed in required_gates.items() if not passed
            )
            summary["diagnostic_gates"] = {
                name: summary["gates"][name]
                for name in sorted(diagnostic_names)
                if name in summary["gates"]
            }
            summary["accepted"] = not failed_gates
            summary["acceptance"] = {
                "mode": "phase8_hard_gate",
                "status": "passed" if not failed_gates else "failed",
                "criteria": sorted(required_gates),
                "failed": failed_gates,
                "diagnostic": sorted(diagnostic_names),
            }
        else:
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
                and summary.get("runtime", {}).get("lpr_evidence", {}).get("valid")
                and summary.get("runtime", {}).get("native_media", {}).get("complete")
            ),
        }
        artifact_names = (
            "runtime-trace.json",
            "runtime-evidence.json",
            "face-annotated-evidence.json",
            "native-media.json",
            "face.json",
            "lpr.json",
            "container-inspect.json",
            "container.log",
            "launcher-state.json",
        ) + (
            ("container-inspect-recognition.json", "container-recognition.log")
            if topology in ("recognition", "tracker")
            else ()
        ) + (
            ("container-inspect-tracker-edge-local.json", "container-tracker-edge-local.log")
            if topology == "tracker"
            else ()
        )
        artifacts_complete = all((output / name).is_file() for name in artifact_names)
        native_media_complete = bool(
            summary.get("runtime", {}).get("native_media", {}).get("complete")
        )
        summary["report"] = {
            "mode": "evidence_only",
            "status": "complete"
            if "error" not in summary and artifacts_complete and native_media_complete
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
                "runtime_output_and_resource_diagnostics",
                "container_logs_and_artifact_hashes",
            ],
        }
        if previous_trace_path is None:
            os.environ.pop("PASSAGE_TRACE_PATH", None)
        else:
            os.environ["PASSAGE_TRACE_PATH"] = previous_trace_path
        if previous_capture_start is None:
            os.environ.pop("PASSAGE_CAPTURE_START_PATH", None)
        else:
            os.environ["PASSAGE_CAPTURE_START_PATH"] = previous_capture_start
        if previous_capture_cutoff is None:
            os.environ.pop("PASSAGE_CAPTURE_CUTOFF_PATH", None)
        else:
            os.environ["PASSAGE_CAPTURE_CUTOFF_PATH"] = previous_capture_cutoff
        if previous_run_id is None:
            os.environ.pop("PASSAGE_RUN_ID", None)
        else:
            os.environ["PASSAGE_RUN_ID"] = previous_run_id
        if previous_source_start_dir is None:
            os.environ.pop("PASSAGE_SOURCE_START_DIR", None)
        else:
            os.environ["PASSAGE_SOURCE_START_DIR"] = previous_source_start_dir
        if previous_report_media is None:
            os.environ.pop("CAMERA_REPORT_MEDIA_DIR", None)
        else:
            os.environ["CAMERA_REPORT_MEDIA_DIR"] = previous_report_media
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
        if previous_recognition_tls is None:
            os.environ.pop("RECOGNITION_TLS_DIR", None)
        else:
            os.environ["RECOGNITION_TLS_DIR"] = previous_recognition_tls
        write_json(output / "summary.json", summary)
        report_path = write_compact_runtime_report(output, summary)
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
