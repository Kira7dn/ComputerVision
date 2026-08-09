"""Opaque typing boundary for the TensorRT module supplied in deployment."""

from typing import Any

def __getattr__(name: str) -> Any: ...
