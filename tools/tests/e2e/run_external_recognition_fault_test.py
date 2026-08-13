"""Thin entrypoint for one real Docker recognition fault scenario."""
# ruff: noqa: I001

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.runtime.validate_platform_runtime import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=(
        "service_restart", "stream_disconnect", "client_disconnect"
    ))
    args = parser.parse_args()
    raise SystemExit(main(["--topology", "recognition", "--fault-scenario", args.scenario]))
