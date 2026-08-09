#!/usr/bin/env python3
"""Collect bounded two-camera native-LPR runtime acceptance evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import websockets
import yaml

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
RUNTIME_CONTAINERS = (
    "frigate",
    "camera-replay-face-camera",
    "camera-replay-car-camera",
)


def get_json(url: str) -> Any:
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def restart_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in RUNTIME_CONTAINERS:
        counts[name] = int(docker("inspect", name, "--format", "{{.RestartCount}}"))
    return counts


def container_started_epoch(name: str) -> float:
    value = docker("inspect", name, "--format", "{{.State.StartedAt}}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def match_passage(
    event_start: float,
    replay_started: float,
    source_duration: float,
    passages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    phase = (event_start - replay_started) % source_duration
    return next(
        (
            passage
            for passage in passages
            if float(passage["start_s"]) <= phase <= float(passage["end_s"])
        ),
        None,
    )


def parse_size(value: str) -> float:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([KMGTP]?i?B)\s*", value)
    if not match:
        raise ValueError(f"Unsupported Docker size: {value}")
    number = float(match.group(1))
    unit = match.group(2)
    factors = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    return number * factors[unit]


def frigate_memory_bytes() -> int:
    raw = docker("stats", "frigate", "--no-stream", "--format", "{{json .}}")
    usage = json.loads(raw)["MemUsage"].split("/")[0].strip()
    return round(parse_size(usage))


def normalized_plate(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def parse_payload(message: dict[str, Any]) -> Any:
    payload = message.get("payload")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload
    return payload


def box_contains_plate(box: list[float], plate_box: list[int]) -> bool:
    x1, y1, x2, y2 = box
    if max(box) <= 2:
        car_box = (
            x1 * FRAME_WIDTH,
            y1 * FRAME_HEIGHT,
            x2 * FRAME_WIDTH,
            y2 * FRAME_HEIGHT,
        )
    else:
        car_box = (x1, y1, x2, y2)
    return (
        car_box[0] <= plate_box[0] <= plate_box[2] <= car_box[2]
        and car_box[1] <= plate_box[1] <= plate_box[3] <= car_box[3]
    )


def database_events(started_epoch: float) -> list[dict[str, Any]]:
    script = """
import json, sqlite3, sys
connection = sqlite3.connect('/config/frigate.db')
rows = connection.execute(
    \"select id,camera,start_time,data from event where camera='car_camera' and start_time >= ?\",
    (float(sys.argv[1]),),
).fetchall()
result = []
for event_id, camera, start_time, raw_data in rows:
    data = json.loads(raw_data or '{}')
    plate = data.get('recognized_license_plate')
    if plate:
        result.append({
            'id': event_id,
            'camera': camera,
            'start_time': start_time,
            'recognized_license_plate': plate,
            'recognized_license_plate_score': data.get('recognized_license_plate_score'),
        })
print(json.dumps(result))
"""
    result = subprocess.run(
        ["docker", "exec", "frigate", "python3", "-c", script, str(started_epoch)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def runtime_lpr_source_contract() -> dict[str, Any]:
    path = "/opt/frigate/frigate/data_processing/common/license_plate/mixin.py"
    script = """
import hashlib, json
path = '/opt/frigate/frigate/data_processing/common/license_plate/mixin.py'
source = open(path, encoding='utf-8').read()
needles = [
    'car = rgb[top:bottom, left:right]',
    'left + plate_box_in_car[0]',
    'top + plate_box_in_car[1]',
    'left + plate_box_in_car[2]',
    'top + plate_box_in_car[3]',
]
print(json.dumps({
    'path': path,
    'sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
    'car_crop_and_plate_offset_present': all(needle in source for needle in needles),
}))
"""
    result = subprocess.run(
        ["docker", "exec", "frigate", "python3", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    contract = json.loads(result.stdout)
    contract["path"] = path
    return contract


def model_download_counts() -> dict[str, int]:
    result = subprocess.run(
        ["docker", "logs", "frigate"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    logs = result.stdout + result.stderr
    filenames = (
        "detection_v5-small.onnx",
        "classification.onnx",
        "recognition_v4.onnx",
        "yolov9-256-license-plates.onnx",
    )
    return {
        filename: len(
            re.findall(
                rf"Downloading model file from:[\s\S]{{0,240}}/{re.escape(filename)}",
                logs,
            )
        )
        for filename in filenames
    }


def load_config_and_source_duration(config_path: str) -> tuple[dict[str, Any], float]:
    with Path(config_path).open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    source = Path(config["runtime"]["replay"]["sources"]["car_camera"])
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return config, float(probe.stdout.strip())


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    started_epoch = time.time()
    started_monotonic = time.monotonic()
    restart_start = restart_counts()
    samples: list[dict[str, Any]] = []
    model_state: dict[str, str] = {}
    lpr_updates: list[dict[str, Any]] = []
    latest_boxes: dict[str, list[float]] = {}
    ws_errors: list[str] = []

    async def receive_ws() -> None:
        try:
            async with websockets.connect(
                args.ws_url,
                additional_headers={"remote-role": "admin"},
                max_size=2**22,
            ) as ws:
                await ws.send(
                    json.dumps({"topic": "modelState", "message": "", "retain": False})
                )
                while time.monotonic() - started_monotonic < args.duration:
                    remaining = args.duration - (time.monotonic() - started_monotonic)
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, remaining)
                        )
                    except asyncio.TimeoutError:
                        break
                    message = json.loads(raw)
                    topic = message.get("topic")
                    payload = parse_payload(message)
                    if topic == "model_state" and isinstance(payload, dict):
                        model_state.update(payload)
                    elif topic == "events" and isinstance(payload, dict):
                        after = payload.get("after") or {}
                        data = after.get("data") or {}
                        if after.get("camera") == "car_camera" and after.get("id"):
                            box = data.get("box") or after.get("box")
                            if isinstance(box, list) and len(box) == 4:
                                latest_boxes[after["id"]] = [
                                    float(value) for value in box
                                ]
                    elif topic == "tracked_object_update" and isinstance(payload, dict):
                        if payload.get("type") != "lpr":
                            continue
                        plate_box = payload.get("plate_box")
                        car_box = latest_boxes.get(str(payload.get("id")))
                        update = dict(payload)
                        update["normalized_plate"] = normalized_plate(
                            str(payload.get("plate", ""))
                        )
                        update["plate_box_valid"] = bool(
                            isinstance(plate_box, list)
                            and len(plate_box) == 4
                            and 0 <= plate_box[0] < plate_box[2] <= FRAME_WIDTH
                            and 0 <= plate_box[1] < plate_box[3] <= FRAME_HEIGHT
                        )
                        update["plate_box_within_observed_car_box"] = (
                            box_contains_plate(car_box, plate_box)
                            if car_box is not None and update["plate_box_valid"]
                            else None
                        )
                        update["observed_car_box"] = car_box
                        lpr_updates.append(update)
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            websockets.WebSocketException,
        ) as error:
            ws_errors.append(f"{type(error).__name__}: {error}")

    ws_task = asyncio.create_task(receive_ws())
    while time.monotonic() - started_monotonic < args.duration:
        stats = get_json(f"{args.api_url}/api/stats")
        samples.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "cameras": {
                    name: {
                        key: stats["cameras"][name].get(key)
                        for key in (
                            "camera_fps",
                            "process_fps",
                            "skipped_fps",
                            "reconnects_last_hour",
                            "stalls_last_hour",
                            "connection_quality",
                        )
                    }
                    for name in ("face_camera", "car_camera")
                },
                "embeddings": stats.get("embeddings", {}),
                "shm_used_mib": stats["service"]["storage"]["/dev/shm"]["used"],
                "shm_total_mib": stats["service"]["storage"]["/dev/shm"]["total"],
                "frigate_memory_bytes": frigate_memory_bytes(),
            }
        )
        remaining = args.duration - (time.monotonic() - started_monotonic)
        if remaining > 0:
            await asyncio.sleep(min(args.interval, remaining))

    await ws_task
    finished_epoch = time.time()
    restart_end = restart_counts()
    events = get_json(
        f"{args.api_url}/api/events?camera=car_camera&label=car&limit=500&include_thumbnails=0"
    )
    accepted_events = []
    passage_plates: dict[tuple[float, ...], list[str]] = defaultdict(list)
    for event in events:
        if float(event.get("start_time") or 0) < started_epoch:
            continue
        data = event.get("data") or {}
        plate = data.get("recognized_license_plate")
        score = data.get("recognized_license_plate_score")
        box = data.get("box")
        if not plate:
            continue
        accepted_events.append(
            {
                "id": event["id"],
                "camera": event["camera"],
                "start_time": event["start_time"],
                "end_time": event.get("end_time"),
                "recognized_license_plate": plate,
                "recognized_license_plate_score": score,
                "box": box,
            }
        )
        if isinstance(box, list) and len(box) == 4:
            # Deterministic replay passages recur at the same image location.
            passage_plates[tuple(round(float(value), 1) for value in box)].append(
                normalized_plate(plate)
            )

    sqlite_events = database_events(started_epoch)
    source_contract = runtime_lpr_source_contract()
    download_counts = model_download_counts()
    sqlite_by_id = {event["id"]: event for event in sqlite_events}
    api_sqlite_matches = []
    for api_event in accepted_events:
        sqlite_event = sqlite_by_id.get(api_event["id"])
        matches = bool(sqlite_event) and all(
            (
                abs(float(api_event[key]) - float(sqlite_event[key])) < 1e-6
                if key in {"start_time", "recognized_license_plate_score"}
                else api_event[key] == sqlite_event[key]
            )
            for key in (
                "camera",
                "start_time",
                "recognized_license_plate",
                "recognized_license_plate_score",
            )
        )
        api_sqlite_matches.append({"id": api_event["id"], "matches": matches})

    consistency = []
    for signature, plates in sorted(passage_plates.items()):
        counts = Counter(plates)
        representative, count = counts.most_common(1)[0]
        consistency.append(
            {
                "passage_signature": signature,
                "ocr_passages": len(plates),
                "representative": representative,
                "consistency": count / len(plates),
                "variants": dict(counts),
            }
        )

    reconnect_delta = {}
    stall_delta = {}
    for camera in ("face_camera", "car_camera"):
        reconnect_delta[camera] = (
            samples[-1]["cameras"][camera]["reconnects_last_hour"]
            - samples[0]["cameras"][camera]["reconnects_last_hour"]
        )
        stall_delta[camera] = (
            samples[-1]["cameras"][camera]["stalls_last_hour"]
            - samples[0]["cameras"][camera]["stalls_last_hour"]
        )

    config, source_duration = await asyncio.to_thread(
        load_config_and_source_duration, args.config
    )
    ground_truth = None
    if args.manifest:
        manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8"))
        passages = manifest["lpr"]["passages"]
        replay_started = container_started_epoch("camera-replay-car-camera")

        def passage_for(event: dict[str, Any]) -> dict[str, Any] | None:
            return match_passage(
                float(event["start_time"]),
                replay_started,
                source_duration,
                passages,
            )

        passage_results = []
        manifest_consistency = []
        for passage in passages:
            detected = [
                event
                for event in events
                if float(event.get("start_time") or 0) >= started_epoch
                and (matched := passage_for(event)) is not None
                and matched["id"] == passage["id"]
            ]
            recognized = [
                event
                for event in accepted_events
                if (matched := passage_for(event)) is not None
                and matched["id"] == passage["id"]
            ]
            plates = [
                normalized_plate(event["recognized_license_plate"])
                for event in recognized
            ]
            counts = Counter(plates)
            representative, count = counts.most_common(1)[0] if counts else (None, 0)
            consistency_value = count / len(plates) if plates else 0.0
            result = {
                "id": passage["id"],
                "detected": bool(detected),
                "recognized_count": len(plates),
                "representative": representative,
                "consistency": consistency_value,
                "variants": dict(counts),
                "readable": bool(passage["readable"]),
                "expected_plate": passage.get("expected_plate"),
                "exact_match": (
                    representative == normalized_plate(str(passage["expected_plate"]))
                    if passage["readable"] and passage.get("expected_plate")
                    else None
                ),
            }
            passage_results.append(result)
            if len(plates) >= 3:
                manifest_consistency.append(
                    {
                        "passage_id": passage["id"],
                        "ocr_passages": len(plates),
                        "representative": representative,
                        "consistency": consistency_value,
                        "variants": dict(counts),
                    }
                )
        consistency = manifest_consistency
        readable_results = [item for item in passage_results if item["readable"]]
        ground_truth = {
            "passage_detection_recall": sum(
                item["detected"] for item in passage_results
            )
            / len(passage_results),
            "lpr_exact_match": (
                sum(item["exact_match"] is True for item in readable_results)
                / len(readable_results)
                if readable_results
                else None
            ),
            "readable_denominator": len(readable_results),
            "passages": passage_results,
        }

    min_camera_fps = {
        camera: min(
            float(sample["cameras"][camera]["camera_fps"]) for sample in samples
        )
        for camera in ("face_camera", "car_camera")
    }
    min_process_fps = {
        camera: min(
            float(sample["cameras"][camera]["process_fps"]) for sample in samples
        )
        for camera in ("face_camera", "car_camera")
    }
    max_shm_ratio = max(
        sample["shm_used_mib"] / sample["shm_total_mib"] for sample in samples
    )
    max_memory = max(sample["frigate_memory_bytes"] for sample in samples)
    qualified_passages = [item for item in consistency if item["ocr_passages"] >= 3]
    required_models = {
        "paddleocr-onnx-detection_v5-small.onnx",
        "paddleocr-onnx-classification.onnx",
        "paddleocr-onnx-recognition_v4.onnx",
        "yolov9_license_plate-yolov9-256-license-plates.onnx",
    }
    checks = {
        "three_replay_rounds": (finished_epoch - started_epoch) / source_duration >= 3,
        "at_least_three_ocr_passages": len(accepted_events) >= 3,
        "ocr_consistency_reported": bool(qualified_passages)
        and all(item.get("consistency") is not None for item in qualified_passages),
        "recognition_scores_reported": bool(accepted_events)
        and all(item.get("recognized_license_plate_score") is not None for item in accepted_events),
        "plate_boxes_valid": bool(lpr_updates)
        and all(item["plate_box_valid"] for item in lpr_updates),
        "plate_box_runtime_source_contract": source_contract[
            "car_crop_and_plate_offset_present"
        ],
        "models_downloaded": required_models.issubset(
            {name for name, state in model_state.items() if state == "downloaded"}
        ),
        "models_loaded_once": all(count <= 1 for count in download_counts.values()),
        "lpr_metrics_present": all(
            key in samples[-1]["embeddings"]
            for key in ("plate_recognition", "yolov9_plate_detection")
        ),
        "api_sqlite_contract": bool(api_sqlite_matches)
        and all(item["matches"] for item in api_sqlite_matches),
        "camera_fps": all(value >= 4.5 for value in min_camera_fps.values()),
        "process_fps": all(value >= 4.5 for value in min_process_fps.values()),
        "restart_delta_zero": restart_start == restart_end,
        "reconnect_delta_zero": all(value == 0 for value in reconnect_delta.values()),
        "stall_delta_zero": all(value == 0 for value in stall_delta.values()),
        "ram_below_7_gib": max_memory <= 7 * 1024**3,
        "shm_below_70_percent": max_shm_ratio < 0.7,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "monitor": {
            "started_epoch": started_epoch,
            "finished_epoch": finished_epoch,
            "duration_seconds": finished_epoch - started_epoch,
            "sample_interval_seconds": args.interval,
            "sample_count": len(samples),
            "replay_source_duration_seconds": source_duration,
            "observed_replay_rounds": (finished_epoch - started_epoch)
            / source_duration,
        },
        "config": {
            "main_model": config["runtime"]["model_path"],
            "lpr": config["lpr"],
            "face_camera": {
                "detect": config["cameras"]["face_camera"]["detect"],
                "face_recognition": config["cameras"]["face_camera"].get(
                    "face_recognition", {}
                ),
                "lpr": config["cameras"]["face_camera"].get("lpr", {}),
            },
            "car_camera": {
                "type": config["cameras"]["car_camera"].get("type", "generic"),
                "detect": config["cameras"]["car_camera"]["detect"],
                "face_recognition": config["cameras"]["car_camera"].get(
                    "face_recognition", {}
                ),
                "lpr": config["cameras"]["car_camera"].get("lpr", {}),
                "objects": config["cameras"]["car_camera"]["objects"],
            },
        },
        "model_state": model_state,
        "model_download_counts": download_counts,
        "runtime_lpr_source_contract": source_contract,
        "ws_errors": ws_errors,
        "lpr_updates": lpr_updates,
        "accepted_api_events": accepted_events,
        "accepted_sqlite_events": sqlite_events,
        "api_sqlite_matches": api_sqlite_matches,
        "ocr_consistency": consistency,
        "ground_truth": ground_truth,
        "stability": {
            "min_camera_fps": min_camera_fps,
            "min_process_fps": min_process_fps,
            "max_frigate_memory_bytes": max_memory,
            "max_shm_ratio": max_shm_ratio,
            "restart_count_start": restart_start,
            "restart_count_end": restart_end,
            "reconnect_delta": reconnect_delta,
            "stall_delta": stall_delta,
            "samples": samples,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "notes": [
            "pending queue depth is not exposed by this Frigate stats schema; detector and enrichment metrics were sampled instead",
            "model_state proves every required model is ready; download counts must stay at zero for a warm cache or one for a cold cache, never repeat in one runtime",
            "OCR consistency is repeatability on deterministic replay passages, not absolute character accuracy without ground truth",
            "event WebSocket boxes are snapshot updates and can be stale relative to LPR timestamps; containment is verified from the running container source contract and live plate boxes are independently bounded to the 1280x720 frame",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--api-url", default="http://127.0.0.1:5001")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:5001/ws")
    parser.add_argument("--config", default="deploy/config.yaml")
    parser.add_argument("--manifest")
    parser.add_argument(
        "--output", default=".tmp/runtime/lpr-acceptance-2cam-720p.json"
    )
    args = parser.parse_args()
    if args.duration <= 0 or args.duration > 90:
        parser.error("LPR measurement must be at most 90 seconds")
    report = asyncio.run(collect(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "passed": report["passed"],
                "checks": report["checks"],
            },
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
