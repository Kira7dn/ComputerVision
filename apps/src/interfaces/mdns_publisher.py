"""Continuously publish vision.local for the current client-facing LAN IP."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from typing import Any

EXCLUDED_INTERFACES = ("lo", "eno1", "l4tbr0", "docker0", "br-", "veth")
POLL_SECONDS = 2.0


def _run_ip(*args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ip", "-j", *args],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _allowed_interface(name: str) -> bool:
    return bool(name) and not any(
        name == value or name.startswith(value)
        for value in EXCLUDED_INTERFACES
    )


def select_lan_address() -> tuple[str, str] | None:
    """Return the lowest-metric client-facing default route and IPv4 address."""
    preferred = os.environ.get("VISION_MDNS_INTERFACE", "").strip()
    routes = _run_ip("route", "show", "default")
    candidates: list[tuple[int, str]] = []
    for route in routes:
        interface = str(route.get("dev") or "")
        if preferred and interface != preferred:
            continue
        if not _allowed_interface(interface):
            continue
        candidates.append((int(route.get("metric") or 0), interface))
    for _metric, interface in sorted(candidates):
        for item in _run_ip("-4", "addr", "show", "dev", interface):
            for address in item.get("addr_info", []) or []:
                if address.get("family") != "inet" or address.get("scope") != "global":
                    continue
                ip = str(address.get("local") or "")
                if ip and not ip.startswith("127."):
                    return interface, ip
    return None


class MdnsPublisher:
    def __init__(self, domain: str = "vision.local") -> None:
        self.domain = domain
        self.process: subprocess.Popen[bytes] | None = None
        self.current: tuple[str, str] | None = None
        self.stopping = False

    def stop_process(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def reconcile(self) -> None:
        selected = select_lan_address()
        if selected == self.current and self.process is not None and self.process.poll() is None:
            return
        self.stop_process()
        self.current = selected
        if selected is None:
            return
        interface, address = selected
        self.process = subprocess.Popen(
            ["avahi-publish-address", "-R", self.domain, address],
        )
        print(f"[vision-mdns] {self.domain} -> {address} interface={interface}", flush=True)

    def run(self) -> None:
        while not self.stopping:
            try:
                self.reconcile()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"[vision-mdns] reconcile failed: {exc}", flush=True)
            time.sleep(POLL_SECONDS)
        self.stop_process()


def main() -> int:
    publisher = MdnsPublisher()

    def stop(_signum: int, _frame: object) -> None:
        publisher.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    publisher.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
