from __future__ import annotations

import json
import os
import subprocess
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
HLS_URL = "http://127.0.0.1:8888/safety_bbox/index.m3u8"
CLK_TCK = os.sysconf("SC_CLK_TCK")
CPU_LOCK = Lock()
PREVIOUS_CPU: tuple[int, int] | None = None
PREVIOUS_PROCESSES: dict[int, tuple[int, float]] = {}


def _proc_cpu() -> tuple[int, int] | None:
    try:
        values = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        numbers = [int(value) for value in values[1:]]
        idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
        return sum(numbers), idle
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _memory() -> dict[str, float | int | None]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0])
    except (FileNotFoundError, ValueError):
        return {"total_mb": None, "used_mb": None, "percent": None}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total_mb": round(total / 1024, 1),
        "used_mb": round(used / 1024, 1),
        "percent": round(used * 100 / total, 1) if total else None,
    }


def _processes() -> list[dict[str, int | str]]:
    result: list[dict[str, int | str]] = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
            if "deepstream_safety/pipeline.py" not in command:
                continue
            stat = (entry / "stat").read_text(encoding="ascii")
            rest = stat[stat.rfind(")") + 2 :].split()
            ticks = int(rest[11]) + int(rest[12])
            start_ticks = int(rest[19])
            rss_kb = 0
            for line in (entry / "status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
            result.append({"pid": pid, "ticks": ticks, "start_ticks": start_ticks, "rss_mb": round(rss_kb / 1024, 1)})
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    return result


def _gpu() -> dict[str, object]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        row = [value.strip() for value in output.splitlines()[0].split(",")]
        return {
            "available": True,
            "name": row[0],
            "utilization_percent": float(row[1]),
            "memory_used_mb": float(row[2]),
            "memory_total_mb": float(row[3]),
            "temperature_c": float(row[4]),
        }
    except (FileNotFoundError, IndexError, subprocess.SubprocessError, ValueError):
        return {"available": False, "name": "unavailable"}


def collect_metrics() -> dict[str, object]:
    global PREVIOUS_CPU
    now = time.monotonic()
    cpu = _proc_cpu()
    with CPU_LOCK:
        cpu_percent = None
        if cpu is not None and PREVIOUS_CPU is not None:
            total_delta = cpu[0] - PREVIOUS_CPU[0]
            idle_delta = cpu[1] - PREVIOUS_CPU[1]
            if total_delta > 0:
                cpu_percent = round((total_delta - idle_delta) * 100 / total_delta, 1)
        PREVIOUS_CPU = cpu

    processes = _processes()
    pipeline = processes[0] if processes else None
    pipeline_cpu = None
    pipeline_age = None
    if pipeline is not None:
        pid = int(pipeline["pid"])
        previous = PREVIOUS_PROCESSES.get(pid)
        if previous is not None:
            tick_delta = int(pipeline["ticks"]) - previous[0]
            elapsed = max(0.001, now - previous[1])
            pipeline_cpu = round(tick_delta / CLK_TCK / elapsed * 100, 1)
        PREVIOUS_PROCESSES[pid] = (int(pipeline["ticks"]), now)
        uptime = float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
        pipeline_age = round(max(0.0, uptime - int(pipeline["start_ticks"]) / CLK_TCK), 1)

    playlist_latency = None
    hls_live = False
    try:
        started = time.perf_counter()
        with urlopen(HLS_URL, timeout=2) as response:
            hls_live = response.status == 200
            response.read(128)
        playlist_latency = round((time.perf_counter() - started) * 1000, 1)
    except OSError:
        pass

    return {
        "timestamp": time.time(),
        "host": {"cpu_percent": cpu_percent, "cpu_cores": os.cpu_count(), "memory": _memory()},
        "gpu": _gpu(),
        "pipeline": {
            "running": pipeline is not None,
            "pid": pipeline["pid"] if pipeline else None,
            "cpu_percent": pipeline_cpu,
            "rss_mb": pipeline["rss_mb"] if pipeline else None,
            "age_seconds": pipeline_age,
        },
        "stream": {"hls_live": hls_live, "playlist_latency_ms": playlist_latency, "hls_url": HLS_URL.replace("127.0.0.1", "localhost")},
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/metrics":
            payload = json.dumps(collect_metrics()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), DashboardHandler).serve_forever()
