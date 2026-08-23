"""Validation helpers for the local fire/smoke YOLO training pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
EXPECTED_NAMES = {0: "fire", 1: "smoke"}


def _as_dataset_root(data_yaml: Path, config: dict[str, Any]) -> Path:
    configured = Path(str(config.get("path", ".")))
    if not configured.is_absolute():
        configured = data_yaml.parent / configured
    return configured.resolve()


def _split_path(root: Path, value: Any, split: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"data.yaml must define a non-empty {split} path")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _names(config: dict[str, Any]) -> dict[int, str]:
    names = config.get("names")
    if isinstance(names, list):
        return {index: str(value) for index, value in enumerate(names)}
    if isinstance(names, dict):
        return {int(key): str(value) for key, value in names.items()}
    raise ValueError("data.yaml must define names as a list or mapping")


def _validate_label(label_path: Path, image_path: Path) -> dict[str, int]:
    counts = {"fire": 0, "smoke": 0}
    for line_number, raw_line in enumerate(
        label_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(
                f"{label_path}:{line_number} must contain class and 4 normalized values"
            )
        try:
            class_id = int(fields[0])
            values = [float(value) for value in fields[1:]]
        except ValueError as exc:
            raise ValueError(f"{label_path}:{line_number} contains non-numeric data") from exc
        if class_id not in EXPECTED_NAMES:
            raise ValueError(f"{label_path}:{line_number} has unsupported class {class_id}")
        if not all(0.0 <= value <= 1.0 for value in values):
            raise ValueError(f"{label_path}:{line_number} has values outside [0, 1]")
        if values[2] <= 0.0 or values[3] <= 0.0:
            raise ValueError(f"{label_path}:{line_number} has an empty bounding box")
        counts[EXPECTED_NAMES[class_id]] += 1
    return counts


def validate_yolo_dataset(
    data_yaml: Path,
    required_splits: tuple[str, ...] = ("train", "val", "test"),
    require_source_manifest: bool = False,
) -> dict[str, Any]:
    """Validate a two-class YOLO dataset and return reproducible counts."""

    data_yaml = data_yaml.resolve()
    if not data_yaml.is_file():
        raise ValueError(f"dataset YAML does not exist: {data_yaml}")
    config = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("dataset YAML must contain a mapping")
    if _names(config) != EXPECTED_NAMES:
        raise ValueError(f"dataset classes must be exactly {EXPECTED_NAMES}")

    report_path = data_yaml.parent / "dataset-report.json"
    if require_source_manifest:
        if not report_path.is_file():
            raise ValueError(f"dataset source manifest is required: {report_path}")
        try:
            report = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid dataset source manifest: {report_path}") from exc
        sources = report.get("sources") if isinstance(report, dict) else None
        if not isinstance(sources, list) or not sources:
            raise ValueError("dataset source manifest must contain a non-empty sources list")
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("dataset source manifest contains an invalid source")
            for key in ("id", "version", "license", "sha256"):
                if not str(source.get(key, "")).strip():
                    raise ValueError(f"dataset source manifest source is missing {key}")

    root = _as_dataset_root(data_yaml, config)
    splits: dict[str, dict[str, Any]] = {}
    for split in required_splits:
        image_dir = _split_path(root, config.get(split), split)
        if not image_dir.is_dir():
            raise ValueError(f"{split} image directory does not exist: {image_dir}")
        label_dir = root / "labels" / split
        if not label_dir.is_dir():
            raise ValueError(f"{split} label directory does not exist: {label_dir}")
        images = sorted(
            path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
        if not images:
            raise ValueError(f"{split} has no images: {image_dir}")

        counts = {"images": len(images), "negative_images": 0, "fire_boxes": 0, "smoke_boxes": 0}
        for image_path in images:
            label_path = label_dir / f"{image_path.stem}.txt"
            if not label_path.is_file():
                raise ValueError(f"missing label for {image_path}: {label_path}")
            labels = _validate_label(label_path, image_path)
            counts["fire_boxes"] += labels["fire"]
            counts["smoke_boxes"] += labels["smoke"]
            if labels["fire"] == 0 and labels["smoke"] == 0:
                counts["negative_images"] += 1
        splits[split] = counts

    for split in ("train", "val"):
        if splits[split]["fire_boxes"] == 0 or splits[split]["smoke_boxes"] == 0:
            raise ValueError(f"{split} must contain both fire and smoke boxes")
    source_records: list[dict[str, Any]] = []
    if report_path.is_file():
        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8")) or {}
        if isinstance(report_data, dict) and isinstance(report_data.get("sources"), list):
            source_records = [item for item in report_data["sources"] if isinstance(item, dict)]
    return {
        "data_yaml": str(data_yaml),
        "dataset_root": str(root),
        "splits": splits,
        "source_manifest": str(report_path) if report_path.is_file() else None,
        "sources": source_records,
    }
