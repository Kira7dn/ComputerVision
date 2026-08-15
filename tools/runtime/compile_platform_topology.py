"""Thin launcher adapter for the shared Frigate topology compiler."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRIGATE_SRC = ROOT / "frigate" / "src"
if str(FRIGATE_SRC) not in sys.path:
    sys.path.insert(0, str(FRIGATE_SRC))

def _load_env_file(path: Path | None) -> None:
    if path is None:
        return
    path = path.resolve()
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            os.environ.setdefault(name, value)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()

    _load_env_file(args.env_file)
    from extension.topology.compiler import compile_topology, materialize_topology
    from extension.topology.loader import PlatformConfigLoader

    source = PlatformConfigLoader.load_source(
        args.config,
        host_labelmap_path=(
            ROOT / "frigate" / "docker" / "main" / "rootfs" / "labelmap" / "coco-80.txt"
        ),
    )
    config = source.config
    plan = compile_topology(config)
    manifest = materialize_topology(source.raw, plan, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
