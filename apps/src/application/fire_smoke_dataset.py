"""Pure helpers for building a reproducible fire/smoke YOLO dataset."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

CLASS_IDS = {"fire": 0, "smoke": 1}
CLASS_NAMES = ["fire", "smoke"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> tuple[str, int]:
    """Hash a source tree without depending on absolute paths or file order."""
    root = root.resolve()
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
        file_count += 1
    return digest.hexdigest(), file_count


def parse_source_spec(value: str) -> tuple[str, Path]:
    """Parse the CLI form ``source-id=path`` without accepting empty values."""
    source_id, separator, raw_path = value.partition("=")
    if not separator or not source_id.strip() or not raw_path.strip():
        raise ValueError("YOLO source must use source-id=path")
    return source_id.strip(), Path(raw_path.strip())


def temporal_split(sample_index: int, block_size: int = 20, phase_blocks: int = 0) -> str:
    """Keep adjacent annotated frames in one deterministic temporal block."""
    if block_size < 1:
        raise ValueError("block_size must be at least 1")
    bucket = (sample_index // block_size + phase_blocks) % 10
    if bucket < 7:
        return "train"
    if bucket < 9:
        return "val"
    return "test"


def yolo_labels(
    objects: list[dict[str, Any]], width: int, height: int
) -> list[str]:
    if width < 1 or height < 1:
        raise ValueError("frame dimensions must be positive")
    labels: list[str] = []
    for item in objects:
        classification = str(item.get("class", ""))
        if classification not in CLASS_IDS:
            continue
        left = min(float(width), max(0.0, float(item["x1"])))
        top = min(float(height), max(0.0, float(item["y1"])))
        right = min(float(width), max(0.0, float(item["x2"])))
        bottom = min(float(height), max(0.0, float(item["y2"])))
        if right <= left or bottom <= top:
            continue
        center_x = ((left + right) / 2.0) / width
        center_y = ((top + bottom) / 2.0) / height
        box_width = (right - left) / width
        box_height = (bottom - top) / height
        labels.append(
            f"{CLASS_IDS[classification]} {center_x:.8f} {center_y:.8f} "
            f"{box_width:.8f} {box_height:.8f}"
        )
    return labels
