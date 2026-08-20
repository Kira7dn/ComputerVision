"""Thin probe contracts; business decisions belong to application services."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ProbeHandler = Callable[[Any], Any]


def dispatch_probe(handler: ProbeHandler, buffer: Any) -> Any:
    return handler(buffer)
