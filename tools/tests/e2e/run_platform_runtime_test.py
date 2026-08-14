"""Default healthy E2E for tracker + Frigate + recognition."""
# ruff: noqa: E402, I001

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.runtime.validate_platform_runtime import main


if __name__ == "__main__":
    raise SystemExit(main(["--topology", "tracker"]))
