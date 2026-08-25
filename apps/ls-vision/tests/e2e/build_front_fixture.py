"""Build a deterministic, calibration-bound CAM_FRONT replay from nuScenes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import cv2


def _load(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list):
        raise ValueError(f"nuScenes table must be a list: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_fixture(
    dataset_root: Path,
    scene_name: str,
    output_directory: Path,
    *,
    width: int = 960,
    height: int = 540,
    fps: int = 20,
) -> dict[str, Any]:
    metadata = dataset_root / "v1.0-mini"
    scenes = {row["name"]: row for row in _load(metadata / "scene.json")}
    scene = scenes.get(scene_name)
    if scene is None:
        raise ValueError(f"nuScenes scene does not exist: {scene_name}")
    samples = {row["token"]: row for row in _load(metadata / "sample.json")}
    sensor_rows = _load(metadata / "sensor.json")
    sensor_tokens = {row["token"] for row in sensor_rows if row["channel"] == "CAM_FRONT"}
    calibrated = {
        row["token"]: row
        for row in _load(metadata / "calibrated_sensor.json")
        if row["sensor_token"] in sensor_tokens
    }

    scene_samples: set[str] = set()
    token = scene["first_sample_token"]
    while token:
        scene_samples.add(token)
        if token == scene["last_sample_token"]:
            break
        token = samples[token]["next"]
    frames = [
        row
        for row in _load(metadata / "sample_data.json")
        if row["sample_token"] in scene_samples
        and row["calibrated_sensor_token"] in calibrated
    ]
    frames.sort(key=lambda row: int(row["timestamp"]))
    if not frames:
        raise ValueError(f"nuScenes scene has no CAM_FRONT frames: {scene_name}")
    calibration_tokens = {row["calibrated_sensor_token"] for row in frames}
    if len(calibration_tokens) != 1:
        raise ValueError("front fixture must use exactly one fixed calibration profile")

    output_directory.mkdir(parents=True, exist_ok=True)
    video_path = output_directory / f"CAM_FRONT-{scene_name}.mp4"
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        float(fps),
        (width, height),
    )
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not create the front fixture video")
    start_us = int(frames[0]["timestamp"])
    end_us = int(frames[-1]["timestamp"])
    tick_us = int(round(1_000_000 / fps))
    source_index = 0
    manifest_frames: list[dict[str, Any]] = []
    previous_token: str | None = None
    try:
        for frame_number, timestamp_us in enumerate(range(start_us, end_us + 1, tick_us)):
            while (
                source_index + 1 < len(frames)
                and abs(int(frames[source_index + 1]["timestamp"]) - timestamp_us)
                <= abs(int(frames[source_index]["timestamp"]) - timestamp_us)
            ):
                source_index += 1
            source = frames[source_index]
            image_path = dataset_root / str(source["filename"])
            image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(f"nuScenes frame is missing: {image_path}")
            resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
            writer.write(resized)
            source_token = str(source["token"])
            manifest_frames.append(
                {
                    "frame_number": frame_number,
                    "pts_seconds": round(frame_number / fps, 6),
                    "source_timestamp_us": int(source["timestamp"]),
                    "sample_token": source["sample_token"],
                    "sample_data_token": source_token,
                    "calibrated_sensor_token": source["calibrated_sensor_token"],
                    "filename": source["filename"],
                    "duplicated_source": source_token == previous_token,
                }
            )
            previous_token = source_token
    finally:
        writer.release()

    calibration_token = next(iter(calibration_tokens))
    calibration = calibrated[calibration_token]
    source_width = int(frames[0]["width"])
    source_height = int(frames[0]["height"])
    scale_x = width / source_width
    scale_y = height / source_height
    intrinsic = calibration["camera_intrinsic"]
    scaled_intrinsic = [
        [float(intrinsic[0][0]) * scale_x, 0.0, float(intrinsic[0][2]) * scale_x],
        [0.0, float(intrinsic[1][1]) * scale_y, float(intrinsic[1][2]) * scale_y],
        [0.0, 0.0, 1.0],
    ]
    manifest = {
        "schema_version": 1,
        "dataset": "nuScenes-v1.0-mini",
        "scene": scene_name,
        "camera": "CAM_FRONT",
        "output": {
            "path": video_path.name,
            "width": width,
            "height": height,
            "fps": fps,
            "frame_count": len(manifest_frames),
            "sha256": _sha256(video_path),
        },
        "calibration": {
            "profile_id": f"nuscenes-{scene_name}-{calibration_token}",
            "calibrated_sensor_token": calibration_token,
            "source_width": width,
            "source_height": height,
            "intrinsics": scaled_intrinsic,
            "translation": calibration["translation"],
            "rotation_quaternion": calibration["rotation"],
            "rpy_calib": [0.0, 0.0, 0.0],
            "artifact_hash": f"nuscenes-calibrated-sensor-{calibration_token}-scaled-{width}x{height}",
            "valid": True,
        },
        "frames": manifest_frames,
    }
    manifest_path = output_directory / f"CAM_FRONT-{scene_name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--scene", default="scene-0061")
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_fixture(args.dataset_root, args.scene, args.output_directory)
    print(json.dumps({"output": manifest["output"], "calibration": manifest["calibration"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
