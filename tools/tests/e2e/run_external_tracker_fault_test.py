"""Stable entrypoint for launcher-owned external tracker fault E2E."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.runtime.validate_platform_runtime import main as validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        required=True,
        choices=(
            "tracker_restart",
            "stream_disconnect",
            "client_disconnect",
            "spool_replay",
            "media_unavailable",
        ),
    )
    args = parser.parse_args()
    return validate(
        ["--topology", "tracker", "--fault-scenario", args.scenario]
    )


if __name__ == "__main__":
    raise SystemExit(main())
