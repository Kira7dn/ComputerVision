#!/usr/bin/env python3
"""Create an isolated native-WSL canary config for one camera."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "app" / "src"))

from bootstrap.config import load_raw_config  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_runtime_shape(path: Path) -> None:
    import onnx

    model = onnx.load(str(path))
    dimensions = [dimension.dim_value for dimension in model.graph.input[0].type.tensor_type.shape.dim]
    if dimensions != [1, 3, 640, 640]:
        raise SystemExit(f"canary candidate must have static input shape [1, 3, 640, 640], got {dimensions}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("app/config/dev.yaml"))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--camera", default="camera_safety")
    parser.add_argument("--output", type=Path, default=Path(".tmp/fire-smoke-canary.yaml"))
    args = parser.parse_args()

    source = args.config.resolve()
    candidate = args.candidate.resolve()
    output = args.output.resolve()
    if not source.is_file() or not candidate.is_file():
        raise SystemExit("config and candidate ONNX must exist")
    if candidate == (ROOT / "assets/models/fire_smoke/best.onnx").resolve():
        raise SystemExit("candidate must not be the production baseline model")
    if not str(candidate).lower().startswith(str((ROOT / ".tmp").resolve()).lower()):
        raise SystemExit("canary candidate must be stored under .tmp")
    _require_runtime_shape(candidate)
    raw = load_raw_config(source)
    if not isinstance(raw, dict):
        raise SystemExit("config root must be a mapping")
    canary = deepcopy(raw)
    cameras = canary.get("cameras") or []
    target = next((camera for camera in cameras if str(camera.get("id")) == args.camera), None)
    if target is None:
        raise SystemExit(f"camera is not configured: {args.camera}")
    fire_override = (
        target.setdefault("analysis", {})
        .setdefault("functions", {})
        .setdefault("fire_smoke", {})
    )
    if not candidate.drive:
        raise SystemExit("canary candidate must be on a Windows drive")
    candidate_wsl = f"/mnt/{candidate.drive[0].lower()}/{candidate.as_posix()[3:].lstrip('/')}"
    fire_override["onnx_path"] = candidate_wsl
    canary.setdefault("runtime", {})["fire_smoke_canary"] = {
        "camera": args.camera,
        "candidate_onnx": str(candidate),
        "candidate_sha256": _sha256(candidate),
        "baseline_onnx": str((ROOT / "assets/models/fire_smoke/best.onnx").resolve()),
        "status": "candidate_only",
    }
    canary["profile"] = "dev"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(canary, sort_keys=False), encoding="utf-8")
    print(json.dumps({"config": str(output), "camera": args.camera, "candidate": str(candidate)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
