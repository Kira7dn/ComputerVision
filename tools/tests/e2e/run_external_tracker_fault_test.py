"""Official Phase 8 fault entrypoint; faults must be implemented by run.ps1."""

from __future__ import annotations

import argparse


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
    parser.parse_args()
    parser.error(
        "Phase 8 fault actions are not enabled in deploy/run.ps1 yet; "
        "no direct Docker fault injection is permitted"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
