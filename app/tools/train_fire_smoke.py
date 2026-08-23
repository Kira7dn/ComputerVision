#!/usr/bin/env python3
"""Train and export a local fire/smoke candidate without touching active models."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "app" / "src"))

from application.fire_smoke_training import validate_yolo_dataset  # noqa: E402


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


def _validation_metrics(validation: object) -> dict[str, object]:
    """Expose explicit per-class metrics; Ultralytics otherwise only reports aggregates."""
    box = getattr(validation, "box", None)
    classes: dict[str, dict[str, float | None]] = {}
    labels = ("fire", "smoke")

    def value(values: object, index: int) -> float | None:
        try:
            return float(values[index])  # type: ignore[index]
        except (IndexError, TypeError, ValueError):
            return None

    for index, label in enumerate(labels):
        classes[label] = {
            "precision": value(getattr(box, "p", ()), index),
            "recall": value(getattr(box, "r", ()), index),
            "map50": value(getattr(box, "ap50", ()), index),
            "map50_95": value(getattr(box, "ap", ()), index),
        }
    return {
        "classes": classes,
        "overall": {
            key: float(item)
            for key, item in getattr(validation, "results_dict", {}).items()
        },
    }


def _benchmark_onnx(path: Path, *, device: str, imgsz: int, samples: int = 20) -> dict[str, object]:
    """Measure the exported runtime contract, returning an explicit unavailable result."""
    try:
        import numpy as np
        import onnxruntime as ort

        available = list(ort.get_available_providers())
        requested = ["CUDAExecutionProvider", "CPUExecutionProvider"] if device != "cpu" else ["CPUExecutionProvider"]
        providers = [item for item in requested if item in available]
        if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in providers:
            providers.append("CPUExecutionProvider")
        if not providers:
            return {"status": "unavailable", "reason": "onnxruntime has no usable provider", "providers": available}
        session = ort.InferenceSession(str(path), providers=providers)
        input_meta = session.get_inputs()[0]
        shape = [int(item) if isinstance(item, int) else imgsz for item in input_meta.shape]
        if len(shape) != 4:
            return {"status": "unavailable", "reason": f"unexpected input shape: {input_meta.shape}"}
        tensor = np.zeros(shape, dtype=np.float32)
        for _ in range(3):
            session.run(None, {input_meta.name: tensor})
        elapsed: list[float] = []
        output_shapes: list[list[int]] = []
        for _ in range(max(1, samples)):
            started = time.perf_counter()
            outputs = session.run(None, {input_meta.name: tensor})
            elapsed.append((time.perf_counter() - started) * 1000.0)
            if not output_shapes:
                output_shapes = [list(output.shape) for output in outputs]
        return {
            "status": "measured",
            "providers": list(session.get_providers()),
            "requested_device": device,
            "input_shape": shape,
            "output_shapes": output_shapes,
            "samples": len(elapsed),
            "p50_ms": statistics.median(elapsed),
            "p95_ms": _percentile(elapsed, 0.95),
        }
    except Exception as exc:  # pragma: no cover - depends on local ONNX runtime installation
        return {"status": "unavailable", "reason": type(exc).__name__}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path(".tmp/fire-smoke-dataset-v1/data.yaml"))
    parser.add_argument(
        "--weights", type=Path, default=Path("assets/models/fire_smoke/best.pt")
    )
    parser.add_argument(
        "--project", type=Path, default=Path(".tmp/fire-smoke-training")
    )
    parser.add_argument("--name", default="candidate-v1")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="auto", help="auto, cpu, or CUDA device index")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--patience", type=int, default=20)
    parser.add_argument("--eval-split", choices=("val", "test"), default="test")
    parser.add_argument("--allow-cpu", action="store_true")
    args = parser.parse_args()

    if not args.data.is_file() or not args.weights.is_file():
        raise SystemExit("dataset YAML and source weights must exist")
    if args.weights.suffix.lower() != ".pt":
        raise SystemExit("source weights must be PyTorch .pt; do not fine-tune an ONNX file")
    try:
        dataset_summary = validate_yolo_dataset(args.data, require_source_manifest=True)
    except ValueError as exc:
        raise SystemExit(f"invalid fire/smoke dataset: {exc}") from exc
    output = args.project / args.name
    if output.exists():
        raise SystemExit(f"training output already exists: {output}")

    import torch

    if args.device == "auto":
        device = "0" if torch.cuda.is_available() else "cpu"
    else:
        device = args.device
    if device == "cpu" and not args.allow_cpu:
        raise SystemExit(
            "CUDA PyTorch is required for this bounded training run; "
            "use --allow-cpu only for an intentionally slow diagnostic."
        )
    if device != "cpu" and not torch.cuda.is_available():
        raise SystemExit("requested CUDA device but this PyTorch installation has no CUDA support")
    from ultralytics import YOLO

    model = YOLO(str(args.weights))
    train_result = model.train(
        data=str(args.data.resolve()),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=device,
        workers=args.workers,
        project=str(args.project.resolve()),
        name=args.name,
        exist_ok=False,
        patience=args.patience,
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
        data=str(args.data.resolve()),
        split=args.eval_split,
        imgsz=args.imgsz,
        device=device,
        batch=args.batch,
        project=str(args.project.resolve()),
        name=f"{args.name}-eval",
        exist_ok=True,
        plots=False,
    )
    exported = Path(
        candidate.export(
            format="onnx",
            imgsz=args.imgsz,
            batch=1,
            dynamic=False,
            simplify=True,
            opset=17,
        )
    )

    latency = _benchmark_onnx(exported, device=device, imgsz=args.imgsz)
    validation_metrics = _validation_metrics(validation)
    report = {
        "data": str(args.data.resolve()),
        "source_weights": str(args.weights.resolve()),
        "dataset_summary": dataset_summary,
        "best_pt": str(best_pt.resolve()),
        "best_pt_sha256": _sha256(best_pt),
        "onnx": str(exported.resolve()),
        "onnx_sha256": _sha256(exported),
        "epochs_requested": args.epochs,
        "device": device,
        "test_metrics": {key: float(value) for key, value in validation.results_dict.items()},
        "validation_metrics": validation_metrics,
        "latency": latency,
        "accepted": False,
        "acceptance_status": "not_accepted",
        "acceptance_scope": "exploratory_public_plus_fixture",
        "acceptance_gates": {
            "dataset_manifest_valid": True,
            "internal_cctv_optional_for_exploratory": True,
            "false_alarms_per_hour_measured": False,
            "baseline_comparison_complete": False,
            "runtime_gpu_parity_complete": False,
            "canary_complete": False,
        },
        "note": "Candidate only; run fixture replay, real-camera evaluation, and GPU parity before promotion.",
    }
    report_path = Path(train_result.save_dir) / "candidate-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "onnx": str(exported)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
