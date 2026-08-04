"""Durable local archive worker owned by the camera runtime."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, source: Path, key: str, checksum: str) -> str:
        target = (self.root / key).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError("object key escapes archive root")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and sha256_file(target) == checksum:
            return checksum
        temporary = target.with_suffix(target.suffix + ".partial")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        return checksum


class UploadWorker:
    extensions = {".dav", ".mp4", ".mov", ".avi", ".mkv", ".ts", ".265", ".h265"}

    def __init__(self, queue, store: LocalObjectStore, prefix: str = "dahua-history"):
        self.queue = queue
        self.store = store
        self.prefix = prefix.strip("/")

    def process_one(self) -> bool:
        item = self.queue.claim()
        if not item:
            return False
        source = self.queue.spool_root / item["relative_path"]
        try:
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]):
                raise RuntimeError("source changed after enqueue")
            checksum = sha256_file(source)
            key = "/".join(part for part in (self.prefix, item["relative_path"]) if part)
            etag = self.store.put(source, key, checksum)
            self.queue.complete(item["id"], key, etag, checksum)
            logging.info("archive complete: %s -> %s", source, key)
        except Exception as exc:
            self.queue.fail(item["id"], exc, item["attempts"] + 1)
            logging.error("archive failed: %s: %s", source, exc)
        return True

    def run(self, poll_seconds: float = 5.0) -> None:
        self.queue.recover_interrupted()
        self.queue.reconcile(self.extensions)
        while True:
            if not self.process_one():
                time.sleep(poll_seconds)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
