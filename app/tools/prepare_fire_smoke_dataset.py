#!/usr/bin/env python3
"""Build a canonical, manifest-backed fire/smoke YOLO dataset.

The command can extract the existing annotated fixture and/or copy already
annotated local YOLO datasets. It deliberately does not download public data:
the caller must provide the pinned dataset version, license and source catalog
entry so a training run remains reproducible and auditable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import cv2
import yaml

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "app" / "src"))

from application.fire_smoke_dataset import (  # noqa: E402
    CLASS_IDS,
    parse_source_spec,
    sha256_file,
    sha256_tree,
    temporal_split,
    yolo_labels,
)

SPLITS = ("train", "val", "test")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _counts() -> dict[str, int]:
    return {"images": 0, "negative_images": 0, "fire_boxes": 0, "smoke_boxes": 0}


def _add_labels(counts: dict[str, int], labels: list[str]) -> None:
    counts["images"] += 1
    counts["negative_images"] += int(not labels)
    counts["fire_boxes"] += sum(line.startswith("0 ") for line in labels)
    counts["smoke_boxes"] += sum(line.startswith("1 ") for line in labels)


def _source_catalog(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("sources", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("source catalog must contain a sources list")
    catalog: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ValueError("each source catalog entry needs an id")
        source_id = str(entry["id"])
        if source_id in catalog:
            raise ValueError(f"duplicate source catalog id: {source_id}")
        for key in ("version", "license"):
            if not str(entry.get(key, "")).strip():
                raise ValueError(f"source {source_id} must define {key}")
        catalog[source_id] = dict(entry)
    return catalog


def _source_record(
    source_id: str,
    source_root: Path,
    metadata: dict[str, Any],
    checksum: str,
    file_count: int,
) -> dict[str, Any]:
    version = str(metadata.get("version", "")).strip()
    license_name = str(metadata.get("license", "")).strip()
    if not version or not license_name:
        raise ValueError(f"source {source_id} needs version and license in --source-catalog")
    record = {
        "id": source_id,
        "kind": str(metadata.get("kind", "yolo")),
        "path": str(source_root.resolve()),
        "version": version,
        "license": license_name,
        "url": str(metadata.get("url", "")),
        "sha256": checksum,
        "file_count": file_count,
    }
    if metadata.get("domain"):
        record["domain"] = str(metadata["domain"])
    if metadata.get("archive_sha256"):
        record["archive_sha256"] = str(metadata["archive_sha256"])
    if metadata.get("notes"):
        record["notes"] = str(metadata["notes"])
    return record


def _fixture_checksum(manifest_path: Path, annotation_path: Path, videos: list[Path]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted([manifest_path, annotation_path, *videos]):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _extract_fixture(
    manifest_path: Path,
    output: Path,
    block_size: int,
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    annotation_path = ROOT / str(manifest["annotation_file"])
    annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
    counts = {split: _counts() for split in SPLITS}
    videos: list[Path] = []
    extracted_sources: list[dict[str, Any]] = []
    global_index = 0
    for case in manifest["cases"]:
        case_id = str(case["id"])
        video_path = ROOT / str(case["video"])
        videos.append(video_path)
        rows = list(annotations[str(case["annotation_key"])])
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"unable to open fixture: {video_path}")
        extracted = 0
        try:
            for annotation in rows:
                frame_number = int(annotation["frame_num"])
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError(f"unable to read {case_id} frame {frame_number}")
                # Phase six keeps both fire and smoke represented in every
                # split for the current fixture while retaining whole blocks.
                split = temporal_split(global_index, block_size, phase_blocks=6)
                global_index += 1
                stem = f"fixture__{case_id}-{frame_number:06d}"
                image_path = output / "images" / split / f"{stem}.jpg"
                label_path = output / "labels" / split / f"{stem}.txt"
                if not cv2.imwrite(str(image_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                    raise RuntimeError(f"unable to write frame: {image_path}")
                height, width = frame.shape[:2]
                labels = yolo_labels(list(annotation.get("objects", [])), width, height)
                label_path.write_text("\n".join(labels), encoding="utf-8")
                _add_labels(counts[split], labels)
                extracted += 1
        finally:
            capture.release()
        extracted_sources.append({"id": case_id, "video": str(video_path), "images": extracted})
    checksum = _fixture_checksum(manifest_path, annotation_path, videos)
    return counts, {
        "id": "internal-fixture",
        "kind": "internal-fixture",
        "path": str(manifest_path.resolve()),
        "version": f"fixture-manifest-{checksum[:12]}",
        "license": "workspace-internal-fixture",
        "url": "",
        "sha256": checksum,
        "file_count": len(set(videos)) + 2,
        "cases": extracted_sources,
    }


def _source_data_yaml(source_root: Path) -> tuple[Path, dict[str, Any]]:
    data_yaml = source_root / "data.yaml"
    if not data_yaml.is_file():
        raise ValueError(f"YOLO source must contain data.yaml: {source_root}")
    config = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError(f"invalid YOLO data.yaml: {data_yaml}")
    names = config.get("names")
    if isinstance(names, list):
        names = {index: str(value) for index, value in enumerate(names)}
    elif isinstance(names, dict):
        names = {int(key): str(value) for key, value in names.items()}
    else:
        raise ValueError(f"YOLO source names are missing: {data_yaml}")
    if set(names.values()) != set(CLASS_IDS):
        raise ValueError(f"YOLO source classes must be exactly {list(CLASS_IDS)}: {data_yaml}")
    config["names"] = names
    configured_value = str(config.get("path", "."))
    configured_root = Path(configured_value)
    if configured_root.is_absolute() and configured_root.is_dir():
        pass
    elif configured_value.startswith(("/", "\\")):
        # On Windows, pathlib treats a POSIX container path such as
        # /kaggle/working/... as rooted but not absolute.  The downloaded
        # archive is authoritative on this machine.
        configured_root = source_root
    else:
        configured_root = source_root / configured_root
    return configured_root.resolve(), config


def _copy_yolo_source(
    source_id: str,
    source_root: Path,
    output: Path,
    metadata: dict[str, Any],
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    dataset_root, config = _source_data_yaml(source_root)
    counts = {split: _counts() for split in SPLITS}
    source_id_to_name = {int(index): name for index, name in config["names"].items()}
    dropped_invalid_boxes = 0
    clipped_boxes = 0
    for split in SPLITS:
        image_dir = Path(str(config.get(split, "")))
        if not image_dir.is_absolute():
            image_dir = dataset_root / image_dir
        image_dir = image_dir.resolve()
        label_dir = dataset_root / "labels" / split
        if not label_dir.is_dir():
            # Ultralytics/Kaggle exports commonly keep labels beside the
            # split's image directory: data/train/{images,labels}.
            label_dir = image_dir.parent / "labels"
        if not image_dir.is_dir() or not label_dir.is_dir():
            raise ValueError(f"source {source_id} must define images and labels for {split}")
        images = sorted(
            path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
        if not images:
            raise ValueError(f"source {source_id} has no {split} images")
        for image in images:
            label_path = label_dir / f"{image.stem}.txt"
            if not label_path.is_file():
                raise ValueError(f"missing label in source {source_id}: {label_path}")
            labels: list[str] = []
            for line_number, raw_line in enumerate(
                label_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                fields = raw_line.split()
                if not fields:
                    continue
                if len(fields) != 5:
                    raise ValueError(f"{label_path}:{line_number} must contain 5 YOLO fields")
                class_index = int(fields[0])
                if class_index not in source_id_to_name:
                    raise ValueError(f"{label_path}:{line_number} has unknown class {class_index}")
                values = [float(value) for value in fields[1:]]
                if not all(value == value and abs(value) != float("inf") for value in values):
                    raise ValueError(f"{label_path}:{line_number} has invalid normalized box")
                if values[2] <= 0 or values[3] <= 0:
                    dropped_invalid_boxes += 1
                    continue
                center_x, center_y, width, height = values
                x1 = max(0.0, center_x - width / 2.0)
                y1 = max(0.0, center_y - height / 2.0)
                x2 = min(1.0, center_x + width / 2.0)
                y2 = min(1.0, center_y + height / 2.0)
                if x2 <= x1 or y2 <= y1:
                    dropped_invalid_boxes += 1
                    continue
                clipped_values = [
                    (x1 + x2) / 2.0,
                    (y1 + y2) / 2.0,
                    x2 - x1,
                    y2 - y1,
                ]
                if clipped_values != values:
                    clipped_boxes += 1
                values = clipped_values
                canonical_id = CLASS_IDS[source_id_to_name[class_index]]
                labels.append(f"{canonical_id} " + " ".join(f"{value:.8f}" for value in values))
            stem = f"{source_id}__{image.stem}"
            shutil.copy2(image, output / "images" / split / f"{stem}{image.suffix.lower()}")
            (output / "labels" / split / f"{stem}.txt").write_text(
                "\n".join(labels), encoding="utf-8"
            )
            _add_labels(counts[split], labels)
    checksum, file_count = sha256_tree(source_root)
    source_record = _source_record(source_id, source_root, metadata, checksum, file_count)
    if dropped_invalid_boxes:
        source_record["dropped_invalid_boxes"] = dropped_invalid_boxes
    if clipped_boxes:
        source_record["clipped_boxes"] = clipped_boxes
    return counts, source_record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("app/tests/fixtures/safety_ground_truth.yaml"))
    parser.add_argument("--no-fixture", action="store_true")
    parser.add_argument("--source-yolo", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--source-catalog", type=Path, help="YAML/JSON catalog with version, license and URL per source")
    parser.add_argument("--output", type=Path, default=Path(".tmp/fire-smoke-dataset-v1"))
    parser.add_argument("--block-size", type=int, default=20)
    args = parser.parse_args()

    if args.no_fixture and not args.source_yolo:
        raise SystemExit("provide at least one --source-yolo or omit --no-fixture")
    if args.block_size < 1:
        raise SystemExit("--block-size must be at least 1")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output already exists; choose a new directory: {output}")
    for split in SPLITS:
        (output / "images" / split).mkdir(parents=True, exist_ok=False)
        (output / "labels" / split).mkdir(parents=True, exist_ok=False)

    catalog = _source_catalog(args.source_catalog)
    total_counts = {split: _counts() for split in SPLITS}
    sources: list[dict[str, Any]] = []
    if not args.no_fixture:
        fixture_counts, fixture_source = _extract_fixture(args.manifest.resolve(), output, args.block_size)
        sources.append(fixture_source)
        for split in SPLITS:
            for key, value in fixture_counts[split].items():
                total_counts[split][key] += value
    seen_ids = {str(source["id"]) for source in sources}
    for raw_spec in args.source_yolo:
        source_id, source_path = parse_source_spec(raw_spec)
        if source_id in seen_ids:
            raise SystemExit(f"duplicate source id: {source_id}")
        source_path = source_path.resolve()
        metadata = catalog.get(source_id)
        if metadata is None:
            raise SystemExit(f"source {source_id} needs an entry in --source-catalog")
        source_counts, source_record = _copy_yolo_source(source_id, source_path, output, metadata)
        sources.append(source_record)
        seen_ids.add(source_id)
        for split in SPLITS:
            for key, value in source_counts[split].items():
                total_counts[split][key] += value

    data_yaml = {
        # Keep the manifest portable between Windows and native WSL.
        "path": ".",
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": ["fire", "smoke"],
    }
    (output / "data.yaml").write_text(yaml.safe_dump(data_yaml, sort_keys=False), encoding="utf-8")
    dataset_report = {
        "schema_version": 2,
        "class_mapping": {"0": "fire", "1": "smoke"},
        "counts": total_counts,
        "sources": sources,
        "split_policy": "source-provided train/val/test; fixture uses deterministic temporal blocks with a fixed phase; no random frame split",
        "source_catalog": str(args.source_catalog.resolve()) if args.source_catalog else None,
        "limitations": [
            "A public source is not accepted without a pinned version and license entry.",
            "Internal CCTV data must be annotated and supplied as a local YOLO source before promotion.",
            "This preparation step does not establish false alarms/hour or runtime GPU acceptance.",
        ],
    }
    (output / "dataset-report.json").write_text(
        json.dumps(dataset_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"output": str(output), "counts": total_counts, "sources": len(sources)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
