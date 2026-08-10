"""Stable, roadmap-independent entrypoint for the Platform runtime evidence test."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.runtime.validate_platform_runtime import main


if __name__ == "__main__":
    raise SystemExit(main())
