"""Validate a bounded multi-camera Frigate face-recognition replay."""

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np


FACE_METRIC_PATTERN = re.compile(r"face_pipeline_metrics (\{.*\})")
SNAPSHOT_METRIC_PATTERN = re.compile(
    r"Face snapshot metrics .*failed=(\d+).*camera_mismatch=(\d+)"
)


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.load(response)


def get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def restart_counts() -> dict[str, int]:
    names = [
        name
        for name in docker("ps", "--format", "{{.Names}}").splitlines()
        if name == "frigate" or name.startswith("camera-replay-")
    ]
    return {
        name: int(docker("inspect", name, "--format", "{{.RestartCount}}"))
        for name in names
    }


def parse_memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)([A-Za-z]+)", value.strip())
    if match is None:
        raise ValueError(f"Unsupported memory value: {value}")
    number, unit = match.groups()
    multipliers = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    return int(float(number) * multipliers[unit])


def resource_sample() -> tuple[int, int]:
    usage = docker("stats", "frigate", "--no-stream", "--format", "{{.MemUsage}}")
    memory_bytes = parse_memory_bytes(usage.split("/")[0])
    shm = docker("exec", "frigate", "df", "-P", "/dev/shm").splitlines()[-1]
    shm_percent = int(shm.split()[4].rstrip("%"))
    return memory_bytes, shm_percent


def parse_runtime_logs(since_epoch: int) -> tuple[list[dict], dict[str, int]]:
    logs = subprocess.run(
        ["docker", "logs", "frigate", "--since", str(since_epoch)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    content = logs.stdout + "\n" + logs.stderr
    face_metrics = []
    snapshot = {"failed": 0, "camera_mismatch": 0}
    for line in content.splitlines():
        match = FACE_METRIC_PATTERN.search(line)
        if match:
            face_metrics.append(json.loads(match.group(1)))
        match = SNAPSHOT_METRIC_PATTERN.search(line)
        if match:
            snapshot["failed"] = max(snapshot["failed"], int(match.group(1)))
            snapshot["camera_mismatch"] = max(
                snapshot["camera_mismatch"], int(match.group(2))
            )
    return face_metrics, snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=60)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    initial_stats = get_json(f"{args.base_url}/api/stats")
    cameras = sorted(initial_stats["cameras"])
    if not cameras:
        raise RuntimeError("No cameras reported by Frigate")
    initial_restarts = restart_counts()
    samples: list[dict] = []
    image_hashes = {camera: [] for camera in cameras}
    image_mse = {camera: [] for camera in cameras}
    previous_images: dict[str, np.ndarray] = {}
    memory_samples = []
    shm_samples = []
    wall_started = time.time()
    started = time.monotonic()
    total_seconds = args.warmup + args.seconds

    while True:
        elapsed = time.monotonic() - started
        stats = get_json(f"{args.base_url}/api/stats")
        sample = {
            "elapsed": round(elapsed, 2),
            "cameras": {
                camera: {
                    key: stats["cameras"][camera][key]
                    for key in (
                        "camera_fps",
                        "process_fps",
                        "skipped_fps",
                        "reconnects_last_hour",
                        "stalls_last_hour",
                    )
                }
                for camera in cameras
            },
            "detectors": {
                name: detector["inference_speed"]
                for name, detector in stats["detectors"].items()
            },
        }
        samples.append(sample)
        print(json.dumps(sample), flush=True)

        if elapsed >= args.warmup:
            memory_bytes, shm_percent = resource_sample()
            memory_samples.append(memory_bytes)
            shm_samples.append(shm_percent)
        if args.warmup <= elapsed <= args.warmup + 60:
            for camera in cameras:
                image_bytes = get_bytes(
                    f"{args.base_url}/api/{camera}/latest.jpg"
                )
                image_hashes[camera].append(
                    hashlib.sha256(image_bytes).hexdigest()
                )
                image = cv2.imdecode(
                    np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR
                )
                if image is None:
                    raise RuntimeError(f"Latest image for {camera} is unreadable")
                if camera in previous_images:
                    image_mse[camera].append(
                        float(
                            np.mean(
                                (image.astype(float) - previous_images[camera]) ** 2
                            )
                        )
                    )
                previous_images[camera] = image.astype(float)

        if elapsed >= total_seconds:
            break
        time.sleep(min(args.interval, total_seconds - elapsed))

    steady = [sample for sample in samples if sample["elapsed"] >= args.warmup]
    face_metrics, snapshot_metrics = parse_runtime_logs(
        int(wall_started + args.warmup)
    )
    final_restarts = restart_counts()
    restart_delta = {
        name: final_restarts.get(name, 0) - initial_restarts.get(name, 0)
        for name in set(initial_restarts) | set(final_restarts)
    }
    publisher_count = len(
        [
            name
            for name in docker("ps", "--format", "{{.Names}}").splitlines()
            if name.startswith("camera-replay-")
        ]
    )
    camera_summary = {}
    for camera in cameras:
        values = [sample["cameras"][camera] for sample in steady]
        camera_summary[camera] = {
            "camera_fps_median": statistics.median(
                value["camera_fps"] for value in values
            ),
            "process_fps_min": min(value["process_fps"] for value in values),
            "skipped_fps_max": max(value["skipped_fps"] for value in values),
            "reconnect_delta": values[-1]["reconnects_last_hour"]
            - values[0]["reconnects_last_hour"],
            "stall_delta": values[-1]["stalls_last_hour"]
            - values[0]["stalls_last_hour"],
            "latest_unique_images": len(set(image_hashes[camera])),
            "live_mse_min": min(image_mse[camera], default=0.0),
        }

    face_limits = {
        "first_attempt_ms_max": 750,
        "confirmed_ms_max": 1500,
        "embedding_ms_max": 200,
    }
    summary = {
        "duration_seconds": round(time.monotonic() - started, 2),
        "warmup_seconds": args.warmup,
        "camera_count": len(cameras),
        "sample_count": len(steady),
        "cameras": camera_summary,
        "detector_inference_ms_max": max(
            value
            for sample in steady
            for value in sample["detectors"].values()
        ),
        "face_metric_windows": len(face_metrics),
        "face_metric_max": {
            key: max((metric.get(key, 0) for metric in face_metrics), default=0)
            for key in (*face_limits, "pending_count")
        },
        "snapshot_metrics": snapshot_metrics,
        "restart_delta": restart_delta,
        "publisher_count": publisher_count,
        "memory_bytes_max": max(memory_samples, default=0),
        "shm_percent_max": max(shm_samples, default=0),
        "samples": samples,
        "face_metrics": face_metrics,
    }
    accepted = (
        all(
            4.5 <= camera["camera_fps_median"] <= 5.5
            and camera["process_fps_min"] >= 4.5
            and camera["skipped_fps_max"] <= 0.5
            and camera["reconnect_delta"] == 0
            and camera["stall_delta"] == 0
            and camera["latest_unique_images"] >= 2
            and camera["live_mse_min"] > 0
            for camera in camera_summary.values()
        )
        and summary["detector_inference_ms_max"] < 200
        and len(face_metrics) > 0
        and all(
            summary["face_metric_max"][key] <= limit
            for key, limit in face_limits.items()
        )
        and face_metrics[-1].get("pending_count", 1) == 0
        and snapshot_metrics["failed"] == 0
        and snapshot_metrics["camera_mismatch"] == 0
        and all(delta == 0 for delta in restart_delta.values())
        and publisher_count == 1
        and summary["memory_bytes_max"] <= 7 * 1024**3
        and summary["shm_percent_max"] < 70
    )
    summary["accepted"] = accepted
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        json.dumps(
            summary
            | {
                "samples": f"{len(samples)} samples",
                "face_metrics": f"{len(face_metrics)} windows",
            }
        ),
        flush=True,
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
