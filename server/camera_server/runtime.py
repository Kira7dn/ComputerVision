"""Single-process supervisor for the camera runtime children."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import json
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, load_settings


@dataclass
class Child:
    name: str
    process: subprocess.Popen


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = Path(__file__).resolve().parents[1]
        self.children: list[Child] = []
        self.stopping = False

    def start(self) -> None:
        self.settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.settings.video_dir.mkdir(parents=True, exist_ok=True)
        self.settings.live_dir.mkdir(parents=True, exist_ok=True)
        self.settings.queue_db.parent.mkdir(parents=True, exist_ok=True)

        self._spawn_media_mtx()
        self._spawn_module("media", "camera_server.media.service", "--public-host", self.settings.public_host)
        self._spawn_module("control", "camera_server.control.service", "--host", "0.0.0.0", "--port", "8081")
        self._spawn_module("ftp", "camera_server.ingest.service", "--public-host", self.settings.public_host)
        self._spawn_module("archive-worker", "camera_server.archive.service")
        logging.info("camera runtime started with %d children", len(self.children))

    def supervise(self) -> int:
        while not self.stopping:
            for child in self.children:
                code = child.process.poll()
                if code is not None:
                    logging.error("child %s exited with code %s", child.name, code)
                    self.stop()
                    return int(code or 1)
            time.sleep(1)
        return 0

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        for child in reversed(self.children):
            if child.process.poll() is None:
                logging.info("stopping %s", child.name)
                child.process.terminate()
        deadline = time.monotonic() + 10
        for child in reversed(self.children):
            remaining = max(0, deadline - time.monotonic())
            try:
                child.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                child.process.kill()

    def _spawn(self, name: str, script: Path, *args: str) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.root) + os.pathsep + environment.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [sys.executable, str(script), *args],
            cwd=str(self.root),
            env=environment,
        )
        self.children.append(Child(name, process))

    def _spawn_module(self, name: str, module: str, *args: str) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.root) + os.pathsep + environment.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [sys.executable, "-m", module, *args],
            cwd=str(self.root),
            env=environment,
        )
        self.children.append(Child(name, process))

    def _spawn_media_mtx(self) -> None:
        executable = os.environ.get("MEDIAMTX_BIN", "mediamtx")
        config = self.root.parent / "deploy" / "mediamtx.yml"
        try:
            process = subprocess.Popen([executable, str(config)], cwd=str(self.root))
        except FileNotFoundError:
            logging.warning("MediaMTX not found; continuing without WebRTC publisher")
            return
        self.children.append(Child("mediamtx", process))


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    settings = load_settings(require_dahua=True)
    lock_path = settings.runtime_dir / "runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            owner = json.loads(lock_path.read_text(encoding="utf-8"))
            os.kill(int(owner["pid"]), 0)
            logging.error("camera runtime already running with pid %s", owner["pid"])
            return 2
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            lock_path.unlink(missing_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    runtime = Runtime(settings)
    signal.signal(signal.SIGINT, lambda *_: runtime.stop())
    signal.signal(signal.SIGTERM, lambda *_: runtime.stop())
    try:
        runtime.start()
        return runtime.supervise()
    finally:
        runtime.stop()
        lock_path.unlink(missing_ok=True)
