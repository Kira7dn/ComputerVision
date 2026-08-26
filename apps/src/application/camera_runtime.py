"""Camera process entrypoint and composition boundary."""

from __future__ import annotations

import argparse
import os
import time
import uuid
from pathlib import Path

from adapters.deepstream.runtime import run_camera_process
from bootstrap.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "CAMERA_CONFIG", "/opt/ls-vision/current/app/config/production.yaml"
            )
        ),
    )
    parser.add_argument("--camera-id", type=str, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--worker-epoch", type=str, default=None)
    parser.add_argument("--config-generation", type=int, default=1)
    parser.add_argument("--expected-plan-hash", type=str, default=None)
    parser.add_argument("--duration", type=int, default=None)
    args = parser.parse_args()
    configure_logging()
    run_id = args.run_id or (
        f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    )
    worker_epoch = args.worker_epoch or f"worker-{uuid.uuid4().hex[:8]}"
    raise SystemExit(
        run_camera_process(
            args.config,
            args.camera_id,
            run_id,
            worker_epoch,
            args.duration,
            args.config_generation,
            args.expected_plan_hash,
        )
    )

if __name__ == "__main__":
    main()
