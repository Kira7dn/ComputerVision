"""Archive worker service entrypoint."""

from __future__ import annotations

from ..config import load_settings
from .queue import UploadQueue
from .worker import LocalObjectStore, UploadWorker


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
