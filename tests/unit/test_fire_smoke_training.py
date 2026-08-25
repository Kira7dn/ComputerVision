import json
from pathlib import Path

import pytest
import yaml

from application.fire_smoke_training import validate_yolo_dataset


def _write_dataset(root: Path) -> Path:
    for split in ("train", "val", "test"):
        (root / "images" / split).mkdir(parents=True)
        (root / "labels" / split).mkdir(parents=True)
        image = root / "images" / split / "sample.jpg"
        image.write_bytes(b"not decoded by validation")
        (root / "labels" / split / "sample.txt").write_text(
            "0 0.5 0.5 0.2 0.2\n1 0.4 0.4 0.1 0.1\n", encoding="utf-8"
        )
    data_yaml = root / "data.yaml"
    data_yaml.write_text(
        yaml.safe_dump(
            {
                "path": str(root),
                "train": "images/train",
                "val": "images/val",
                "test": "images/test",
                "names": ["fire", "smoke"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return data_yaml


def test_validate_yolo_dataset_returns_split_counts(tmp_path: Path) -> None:
    summary = validate_yolo_dataset(_write_dataset(tmp_path / "dataset"))

    assert summary["splits"]["train"] == {
        "images": 1,
        "negative_images": 0,
        "fire_boxes": 1,
        "smoke_boxes": 1,
    }


def test_validate_yolo_dataset_rejects_missing_class(tmp_path: Path) -> None:
    data_yaml = _write_dataset(tmp_path / "dataset")
    config = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    config["names"] = ["fire"]
    data_yaml.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="classes"):
        validate_yolo_dataset(data_yaml)


def test_training_validation_requires_source_manifest_for_candidates(tmp_path: Path) -> None:
    data_yaml = _write_dataset(tmp_path / "dataset")
    with pytest.raises(ValueError, match="source manifest"):
        validate_yolo_dataset(data_yaml, require_source_manifest=True)

    (data_yaml.parent / "dataset-report.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "internal",
                        "version": "v1",
                        "license": "workspace",
                        "sha256": "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    summary = validate_yolo_dataset(data_yaml, require_source_manifest=True)
    assert summary["source_manifest"].endswith("dataset-report.json")
