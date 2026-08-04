"""Archive worker service entrypoint."""

from __future__ import annotations

import sys

from .worker import UploadWorker, LocalObjectStore
from .queue import UploadQueue
from ..config import load_settings


def main() -> None:
    settings = load_settings(require_dahua=False)
    worker = UploadWorker(
        UploadQueue(settings.queue_db, settings.video_dir),
        LocalObjectStore(settings.runtime_dir / "archive"),
        "dahua-history",
    )
    worker.run()


if __name__ == "__main__":
    main()
