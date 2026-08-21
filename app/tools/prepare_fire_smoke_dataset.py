#!/usr/bin/env python3
"""Extract annotated fixture frames into a temporal-blocked YOLO dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import yaml

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "app" / "src"))

from application.fire_smoke_dataset import temporal_split, yolo_labels  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("app/tests/fixtures/safety_ground_truth.yaml"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".tmp/fire-smoke-dataset-v1")
    )
    parser.add_argument("--block-size", type=int, default=20)
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output already exists; choose a new directory: {output}")
    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) or {}
    annotation_path = ROOT / str(manifest["annotation_file"])
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    for split in ("train", "val", "test"):
        (output / "images" / split).mkdir(parents=True, exist_ok=False)
        (output / "labels" / split).mkdir(parents=True, exist_ok=False)

    counts = {
        split: {"images": 0, "negative_images": 0, "fire_boxes": 0, "smoke_boxes": 0}
        for split in ("train", "val", "test")
    }
    sources: list[dict[str, object]] = []
    for case in manifest["cases"]:
        case_id = str(case["id"])
        video_path = ROOT / str(case["video"])
        rows = list(annotations[str(case["annotation_key"])] )
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"unable to open fixture: {video_path}")
        extracted = 0
        try:
            for index, annotation in enumerate(rows):
                frame_number = int(annotation["frame_num"])
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError(
                        f"unable to read {case_id} frame {frame_number}"
                    )
                split = temporal_split(index, args.block_size)
                stem = f"{case_id}-{frame_number:06d}"
                image_path = output / "images" / split / f"{stem}.jpg"
                label_path = output / "labels" / split / f"{stem}.txt"
                if not cv2.imwrite(
                    str(image_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]
                ):
                    raise RuntimeError(f"unable to write frame: {image_path}")
                height, width = frame.shape[:2]
                labels = yolo_labels(
                    list(annotation.get("objects", [])), width, height
                )
                label_path.write_text("\n".join(labels), encoding="utf-8")
                counts[split]["images"] += 1
                counts[split]["negative_images"] += int(not labels)
                counts[split]["fire_boxes"] += sum(
                    line.startswith("0 ") for line in labels
                )
                counts[split]["smoke_boxes"] += sum(
                    line.startswith("1 ") for line in labels
                )
                extracted += 1
        finally:
            capture.release()
        sources.append(
            {"id": case_id, "video": str(video_path), "images": extracted}
        )

    data_yaml = {
        "path": str(output),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {0: "fire", 1: "smoke"},
    }
    (output / "data.yaml").write_text(
        yaml.safe_dump(data_yaml, sort_keys=False), encoding="utf-8"
    )
    dataset_report = {
        "source_manifest": str(args.manifest.resolve()),
        "annotation_file": str(annotation_path.resolve()),
        "block_size": args.block_size,
        "counts": counts,
        "sources": sources,
        "limitations": [
            "Only three fixture videos are available.",
            "Temporal blocks prevent adjacent-frame leakage but scenes still overlap across splits.",
            "This dataset cannot establish real-camera production accuracy.",
        ],
    }
    (output / "dataset-report.json").write_text(
        json.dumps(dataset_report, indent=2), encoding="utf-8"
    )
    print(json.dumps({"output": str(output), "counts": counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
