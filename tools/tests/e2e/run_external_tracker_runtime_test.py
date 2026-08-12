"""Official Phase 8 healthy entrypoint; orchestration remains in run.ps1."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="deploy/config.tracker.yaml")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    launcher = root / "deploy" / "run.ps1"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(launcher),
            "acceptance-start",
            "-ConfigFile",
            str(root / args.config),
        ],
        cwd=root,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
