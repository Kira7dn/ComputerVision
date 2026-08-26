"""Stable API v1 response contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ApiError:
    request_id: str
    code: str
    message: str

    def json(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Accepted:
    request_id: str
    status: str = "accepted"
    state_revision: int = 0

    def json(self) -> dict[str, Any]:
        return asdict(self)
