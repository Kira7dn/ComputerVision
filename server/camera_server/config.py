"""Validated, environment-driven configuration shared by camera services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if value else value


def required_secret(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    runtime_dir: Path
    dahua_host: str
    dahua_username: str
    dahua_password: str
    media_url: str
    control_token: str | None
    public_host: str

    @property
    def video_dir(self) -> Path:
        return self.runtime_dir / "uploads" / "videos"

    @property
    def live_dir(self) -> Path:
        return self.runtime_dir / "uploads" / "live"

    @property
    def queue_db(self) -> Path:
        return self.runtime_dir / "queue" / "cloud_queue.sqlite3"


def load_settings(*, require_dahua: bool = True) -> Settings:
    default_runtime = Path(os.environ.get("PROGRAMDATA", Path.cwd())) / "Letron" / "Camera"
    runtime = Path(_env("CAMERA_RUNTIME_DIR", str(default_runtime))).expanduser().resolve()
    password = required_secret("DAHUA_PASSWORD") if require_dahua else (_env("DAHUA_PASSWORD") or "")
    return Settings(
        runtime_dir=runtime,
        dahua_host=_env("DAHUA_HOST", "192.168.100.229") or "",
        dahua_username=_env("DAHUA_USERNAME", "admin") or "",
        dahua_password=password,
        media_url=_env("MEDIA_URL", "http://127.0.0.1:8080") or "",
        control_token=_env("CONTROL_API_TOKEN"),
        public_host=_env("PUBLIC_HOST", "127.0.0.1") or "",
    )
