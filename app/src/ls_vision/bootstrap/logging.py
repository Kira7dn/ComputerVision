"""Consistent process logging bootstrap."""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("CAMERA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
