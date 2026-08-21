#!/usr/bin/env python3
"""Fine-tune and export a fixture candidate without touching active models."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data", type=Path, default=Path(".tmp/fire-smoke-dataset-v1/data.yaml")
    )
    parser.add_argument(
        "--weights", type=Path, default=Path("assets/models/fire_smoke/best.pt")
    )
    parser.add_argument(
        "--project", type=Path, default=Path(".tmp/fire-smoke-training")
    )
    parser.add_argument("--name", default="candidate-v1")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--device", default="0")
    parser.add_argument("--allow-cpu", action="store_true")
    args = parser.parse_args()

    if not args.data.is_file() or not args.weights.is_file():
        raise SystemExit("dataset YAML and source weights must exist")
    output = args.project / args.name
    if output.exists():
        raise SystemExit(f"training output already exists: {output}")

    import torch

    if not torch.cuda.is_available() and not args.allow_cpu:
        raise SystemExit(
            "CUDA PyTorch is required for this bounded training run; "
            "use --allow-cpu only for an intentionally slow diagnostic."
        )
    from ultralytics import YOLO

    model = YOLO(str(args.weights))
    train_result = model.train(
        data=str(args.data.resolve()),
        epochs=args.epochs,
        imgsz=640,
        batch=args.batch,
        device=args.device if torch.cuda.is_available() else "cpu",
        workers=2,
        project=str(args.project.resolve()),
        name=args.name,
        exist_ok=False,
        patience=10,
        seed=42,
        deterministic=True,
        close_mosaic=5,
        cache=False,
    )
    best_pt = Path(train_result.save_dir) / "weights" / "best.pt"
    if not best_pt.is_file():
        raise RuntimeError(f"training completed without best.pt: {best_pt}")
    candidate = YOLO(str(best_pt))
    validation = candidate.val(
        data=str(args.data.resolve()), split="test", imgsz=640, device=args.device
    )
    exported = Path(
        candidate.export(format="onnx", imgsz=640, batch=1, dynamic=False, simplify=True)
    )
    report = {
        "data": str(args.data.resolve()),
        "source_weights": str(args.weights.resolve()),
        "best_pt": str(best_pt.resolve()),
        "best_pt_sha256": _sha256(best_pt),
        "onnx": str(exported.resolve()),
        "onnx_sha256": _sha256(exported),
        "epochs_requested": args.epochs,
        "device": args.device if torch.cuda.is_available() else "cpu",
        "test_metrics": {
            key: float(value)
            for key, value in validation.results_dict.items()
        },
        "accepted": None,
        "note": "Candidate only; run fixture replay and GPU parity before promotion.",
    }
    report_path = Path(train_result.save_dir) / "candidate-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "onnx": str(exported)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
