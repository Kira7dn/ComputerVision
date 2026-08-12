"""Train and export a native camera-1 YOLOv8 LPR candidate.

The command intentionally requires PyTorch weights and real YOLO labels. It
never edits the active Frigate model or adds a score multiplier.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ultralytics import YOLO


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / ".tmp/lpr-dataset-v2"
DEFAULT_OUTPUT = ROOT / ".tmp/assets/models/native-lpr-finetuned"


def images(root: Path, split: str) -> list[Path]:
    return [p for ext in ("*.jpg", "*.jpeg", "*.png") for p in (root / "images" / split).glob(ext)]


def labels_for(root: Path, image: Path, split: str) -> Path:
    return root / "labels" / split / f"{image.stem}.txt"


def check_dataset(root: Path) -> dict:
    if not root.exists():
        raise SystemExit(f"Dataset does not exist: {root}")
    counts = {}
    missing = []
    for split in ("train", "val", "test"):
        split_images = images(root, split)
        counts[split] = len(split_images)
        for image in split_images:
            label = labels_for(root, image, split)
            if not label.exists() or not label.read_text(encoding="utf-8").strip():
                missing.append(str(label))
    if any(counts[split] == 0 for split in ("train", "val", "test")):
        raise SystemExit(f"Each split needs images; found {counts}")
    if missing:
        preview = "\n".join(missing[:10])
        raise SystemExit(
            "Real YOLO annotations are required for every image. Missing/empty labels:\n"
            f"{preview}\nAnnotate car and license_plate before training."
        )
    invalid = []
    for split in ("train", "val", "test"):
        for label in (root / "labels" / split).glob("*.txt"):
            for line_number, line in enumerate(label.read_text(encoding="utf-8").splitlines(), 1):
                fields = line.split()
                try:
                    values = [float(value) for value in fields]
                except ValueError:
                    values = []
                if len(values) != 5 or int(values[0]) not in (0, 1) or any(not 0 <= value <= 1 for value in values[1:]):
                    invalid.append(f"{label}:{line_number}")
    if invalid:
        raise SystemExit("Invalid YOLO labels (class 0=car, 1=license_plate):\n" + "\n".join(invalid[:20]))
    return counts


def write_data_yaml(root: Path, output: Path) -> Path:
    data = output / "data.yaml"
    data.write_text(
        f"path: {root.resolve().as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        "  0: car\n"
        "  1: license_plate\n",
        encoding="utf-8",
    )
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True, help="PyTorch YOLOv8n weights; do not pass an ONNX file")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="0", help="0 for CUDA, cpu for CPU")
    args = parser.parse_args()
    weights = args.weights.resolve()
    if weights.suffix.lower() != ".pt":
        raise SystemExit("--weights must be a PyTorch .pt file; direct ONNX fine-tuning is unsupported")
    if not weights.exists():
        raise SystemExit(f"Weights do not exist: {weights}")

    counts = check_dataset(args.data_root.resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    data_yaml = write_data_yaml(args.data_root.resolve(), args.output)
    model = YOLO(str(weights))
    train_result = model.train(
        data=str(data_yaml),
        imgsz=args.imgsz,
        epochs=args.epochs,
        batch=args.batch,
        device=args.device,
        workers=4,
        project=str(args.output.parent),
        name=args.output.name,
        exist_ok=True,
        pretrained=True,
        seed=0,
        deterministic=True,
        val=True,
        plots=True,
        nms=False,
    )
    best = Path(train_result.save_dir) / "weights" / "best.pt"
    if not best.exists():
        raise SystemExit(f"Training completed without best.pt: {best}")
    trained = YOLO(str(best))
    validation = trained.val(data=str(data_yaml), split="test", imgsz=args.imgsz, batch=args.batch, device=args.device, plots=True)
    exported = Path(trained.export(format="onnx", imgsz=args.imgsz, batch=1, dynamic=False, simplify=False, opset=12, nms=False))
    candidate = args.output / "native-lpr-finetuned.onnx"
    if exported.resolve() != candidate.resolve():
        shutil.copy2(exported, candidate)
    manifest = {
        "weights": str(weights),
        "best_pt": str(best),
        "candidate_onnx": str(candidate),
        "data_yaml": str(data_yaml),
        "counts": counts,
        "classes": {"0": "car", "1": "license_plate"},
        "export": {"format": "onnx", "imgsz": args.imgsz, "batch": 1, "dynamic": False, "nms": False, "score_multiplier": None},
        "test_metrics": {"map50": float(validation.box.map50), "map50_95": float(validation.box.map)},
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
