"""Collect a bounded Jetson front-camera shadow acceptance report."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = index - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 3)


def status_root(profile: str) -> str:
    roots = {
        "production": "/opt/ls-vision",
        "development": "/opt/ls-vision-dev",
    }
    try:
        return roots[profile]
    except KeyError as exc:
        raise ValueError(f"unknown front-camera profile: {profile}") from exc


def remote_snapshot(alias: str, profile: str) -> dict[str, Any]:
    root = status_root(profile)
    command = "\n".join(
        (
            f"cat {root}/data/status/camera_front.json",
            "printf '\\n'",
            f"cat {root}/data/status/DMS.json",
            "printf '\\n'",
            "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/health/ready",
            "printf '\\n'",
            "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/camera_front/index.m3u8",
            "printf '\\n'",
            "timeout 2 tegrastats --interval 100 2>/dev/null | head -n 1",
        )
    )
    result = subprocess.run(
        ["ssh", alias, command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) < 5:
        raise RuntimeError(result.stderr.strip() or f"incomplete snapshot: {lines!r}")
    front, dms = (json.loads(lines[index]) for index in range(2))
    tegra = lines[-1]
    return {
        "sampled_at": time.time(),
        "front": front,
        "dms": dms,
        "ready_http": int(lines[2]),
        "front_hls_http": int(lines[3]),
        "tegrastats": tegra,
    }


def number(pattern: str, value: str) -> float | None:
    match = re.search(pattern, value)
    return float(match.group(1)) if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jetson", default="jetson-nano")
    parser.add_argument(
        "--profile", choices=("production", "development"), default="production"
    )
    parser.add_argument("--warmup", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    samples: list[dict[str, Any]] = []
    failures: list[str] = []
    raw_report = args.report.with_name(f"{args.report.stem}-samples.jsonl")
    raw_report.parent.mkdir(parents=True, exist_ok=True)
    raw_report.write_text("", encoding="utf-8")
    if args.warmup > 0:
        print(f"front-camera Jetson warmup: {args.warmup:.0f}s", flush=True)
        time.sleep(args.warmup)
    started = time.monotonic()
    next_progress = 30.0
    while time.monotonic() - started < args.duration:
        cycle_started = time.monotonic()
        try:
            sample = remote_snapshot(args.jetson, args.profile)
            samples.append(sample)
            with raw_report.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        except Exception as exc:
            failures.append(str(exc))
        elapsed = time.monotonic() - started
        if elapsed >= next_progress:
            print(f"front-camera Jetson collection: {elapsed:.0f}/{args.duration:.0f}s", flush=True)
            next_progress += 30.0
        time.sleep(max(0.0, args.interval - (time.monotonic() - cycle_started)))

    if len(samples) < 2:
        raise RuntimeError(f"not enough valid samples: {failures}")
    first, last = samples[0], samples[-1]
    front_first, front_last = first["front"], last["front"]
    # Status timestamps and counters are written atomically by the worker;
    # using their interval avoids bias from SSH/tegrastats collection time.
    elapsed = float(front_last["updated_at"]) - float(front_first["updated_at"])
    frame_delta = 0
    processed_delta = 0
    enqueued_delta = 0
    dropped_delta = 0
    for previous, current in zip(samples, samples[1:], strict=False):
        previous_front = previous["front"]
        current_front = current["front"]
        if previous_front["worker_epoch"] != current_front["worker_epoch"]:
            continue
        previous_flow = previous_front["analysis_flow"]["functions"]["front_assistance"]
        current_flow = current_front["analysis_flow"]["functions"]["front_assistance"]
        frame_delta += max(0, int(current_front["frame_count"]) - int(previous_front["frame_count"]))
        processed_delta += max(0, int(current_flow["processed"]) - int(previous_flow["processed"]))
        enqueued_delta += max(0, int(current_flow["enqueued"]) - int(previous_flow["enqueued"]))
        dropped_delta += max(0, int(current_flow["dropped"]) - int(previous_flow["dropped"]))
    inference_ms = [
        float(front_debug["inference_ms"])
        for sample in samples
        if (front_debug := sample["front"]["analysis_debug"].get("front_assistance", {}))
        and front_debug.get("inference_ms") is not None
    ]
    output_age_ms = [
        max(
            0.0,
            (
                float(sample["front"]["updated_at"])
                - float(sample["front"]["last_output_at"])
            )
            * 1000.0,
        )
        for sample in samples
        if sample["front"].get("last_output_at")
    ]
    gpu_percent = [
        value
        for sample in samples
        if (value := number(r"GR3D_FREQ\s+(\d+(?:\.\d+)?)%", sample["tegrastats"]))
        is not None
    ]
    ram_used_mb = [
        value
        for sample in samples
        if (value := number(r"RAM\s+(\d+(?:\.\d+)?)/", sample["tegrastats"]))
        is not None
    ]
    swap_used_mb = [
        value
        for sample in samples
        if (value := number(r"SWAP\s+(\d+(?:\.\d+)?)/", sample["tegrastats"]))
        is not None
    ]
    temperatures = [
        float(value)
        for sample in samples
        for value in re.findall(r"@([0-9]+(?:\.[0-9]+)?)C", sample["tegrastats"])
    ]
    providers = sorted(
        {
            str(front_debug["provider"])
            for sample in samples
            if (front_debug := sample["front"]["analysis_debug"].get("front_assistance", {}))
            and front_debug.get("provider")
        }
    )
    model_hashes = sorted(
        {
            str(front_debug["model_hash"])
            for sample in samples
            if (front_debug := sample["front"]["analysis_debug"].get("front_assistance", {}))
            and front_debug.get("model_hash")
        }
    )
    calibration_hashes = sorted(
        {
            str(front_debug["calibration_hash"])
            for sample in samples
            if (front_debug := sample["front"]["analysis_debug"].get("front_assistance", {}))
            and front_debug.get("calibration_hash")
        }
    )
    same_workers = (
        front_first["pid"] == front_last["pid"]
        and front_first["worker_epoch"] == front_last["worker_epoch"]
        and first["dms"]["pid"] == last["dms"]["pid"]
        and first["dms"]["worker_epoch"] == last["dms"]["worker_epoch"]
    )
    gates = {
        "duration_10_minutes": elapsed >= 590.0,
        "front_and_dms_ready": all(
            sample["ready_http"] == 200
            and sample["front"]["analysis_debug"].get("front_assistance", {}).get("readiness")
            == "ready"
            and sample["dms"].get("last_output_at") is not None
            for sample in samples
        ),
        "front_hls": all(sample["front_hls_http"] == 200 for sample in samples),
        "workers_not_restarted": same_workers,
        "no_runtime_failures": all(
            not sample["front"].get("analysis_error")
            and not sample["dms"].get("analysis_error")
            for sample in samples
        ),
        "gpu_provider": providers in (["CUDAExecutionProvider"], ["TensorrtExecutionProvider"]),
        "model_hash_stable": len(model_hashes) == 1,
        "calibration_hash_stable": len(calibration_hashes) == 1,
        "model_tick_19hz": processed_delta / elapsed >= 19.0,
        "inference_p95_50ms": (percentile(inference_ms, 0.95) or math.inf) <= 50.0,
        "inference_p99_75ms": (percentile(inference_ms, 0.99) or math.inf) <= 75.0,
        "output_age_p95_150ms": (percentile(output_age_ms, 0.95) or math.inf) <= 150.0,
        "drop_ratio_below_1pct": dropped_delta / max(1, enqueued_delta) < 0.01,
        "gpu_p95_90pct": (percentile(gpu_percent, 0.95) or math.inf) <= 90.0,
        "no_swap": max(swap_used_mb, default=math.inf) == 0.0,
        "no_sample_failures": not failures,
    }
    report = {
        "schema_version": 1,
        "profile": f"front-camera-jetson-{args.profile}",
        "accepted": all(gates.values()),
        "production_accepted": False,
        "jetson": args.jetson,
        "duration_seconds": round(elapsed, 3),
        "warmup_seconds": args.warmup,
        "sample_count": len(samples),
        "sample_failures": failures,
        "runtime": {
            "run_id": front_last["run_id"],
            "front_worker_epoch": front_last["worker_epoch"],
            "front_pid": front_last["pid"],
            "dms_worker_epoch": last["dms"]["worker_epoch"],
            "dms_pid": last["dms"]["pid"],
            "provider": providers,
            "model_hash": model_hashes,
            "calibration_hash": calibration_hashes,
            "source_fps": round(frame_delta / elapsed, 3),
            "model_tick_hz": round(processed_delta / elapsed, 3),
            "enqueued": enqueued_delta,
            "processed": processed_delta,
            "dropped": dropped_delta,
            "drop_ratio": round(dropped_delta / max(1, enqueued_delta), 6),
        },
        "latency_ms": {
            "inference_p50": percentile(inference_ms, 0.50),
            "inference_p95": percentile(inference_ms, 0.95),
            "inference_p99": percentile(inference_ms, 0.99),
            "output_age_p95": percentile(output_age_ms, 0.95),
        },
        "resources": {
            "gpu_percent_p95": percentile(gpu_percent, 0.95),
            "ram_used_mb_max": max(ram_used_mb, default=None),
            "swap_used_mb_max": max(swap_used_mb, default=None),
            "temperature_c_max": max(temperatures, default=None),
        },
        "gates": gates,
        "notes": [
            "Development sync-lite fixture is transcoded to 20 FPS; source_fps includes repeated frames and is not real-camera FPS.",
            "TensorRT 10.3 could not build this ONNX graph; the measured provider is the configured CUDA fallback.",
            "Production acceptance remains false until a calibrated vehicle camera and labeled road replay pass.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
