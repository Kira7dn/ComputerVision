"""Validate sustained Frigate face-camera runtime metrics and live images."""

import argparse
import hashlib
import json
import statistics
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base = "http://127.0.0.1:5001"
    samples = []
    image_hashes = []
    image_mse = []
    previous_image = None
    started = time.monotonic()

    while True:
        elapsed = time.monotonic() - started
        stats = get_json(f"{base}/api/stats")
        streams = get_json(f"{base}/api/go2rtc/streams")
        if "face_camera" not in streams:
            raise RuntimeError("go2rtc face_camera stream is missing")

        camera = stats["cameras"]["face_camera"]
        sample = {
            "elapsed": round(elapsed, 2),
            "camera_fps": camera["camera_fps"],
            "process_fps": camera["process_fps"],
            "skipped_fps": camera["skipped_fps"],
            "inference_ms": stats["detectors"]["onnx"]["inference_speed"],
        }
        samples.append(sample)
        print(json.dumps(sample), flush=True)

        if elapsed <= 60:
            image_bytes = get_bytes(f"{base}/api/face_camera/latest.jpg")
            image_hashes.append(hashlib.sha256(image_bytes).hexdigest())
            image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError("latest face_camera image is unreadable")
            if previous_image is not None:
                image_mse.append(
                    float(np.mean((image.astype(float) - previous_image) ** 2))
                )
            previous_image = image.astype(float)

        if elapsed >= args.seconds:
            break
        time.sleep(min(args.interval, args.seconds - elapsed))

    steady = [sample for sample in samples if sample["elapsed"] >= 30]
    summary = {
        "duration_seconds": round(time.monotonic() - started, 2),
        "sample_count": len(samples),
        "camera_fps_median": statistics.median(s["camera_fps"] for s in steady),
        "process_fps_min": min(s["process_fps"] for s in steady),
        "skipped_fps_max": max(s["skipped_fps"] for s in steady),
        "inference_ms_max": max(s["inference_ms"] for s in steady),
        "latest_unique_images": len(set(image_hashes)),
        "live_mse_min": min(image_mse),
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary | {"samples": f"{len(samples)} samples"}), flush=True)

    accepted = (
        4.5 <= summary["camera_fps_median"] <= 5.5
        and summary["process_fps_min"] >= 4.5
        and summary["skipped_fps_max"] <= 0.5
        and summary["inference_ms_max"] < 200
        and summary["latest_unique_images"] >= 2
        and summary["live_mse_min"] > 0
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
