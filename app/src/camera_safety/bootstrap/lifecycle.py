"""Lifecycle helpers used by service/container entrypoints."""

from __future__ import annotations

import signal
from collections.abc import Callable
from types import FrameType


def install_shutdown_handlers(stop: Callable[[], None]) -> None:
    def handler(_signum: int, _frame: FrameType | None) -> None:
        stop()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
