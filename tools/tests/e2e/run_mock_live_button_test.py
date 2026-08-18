"""Browser E2E for the dashboard mock-input lifecycle.

The test uses the real dashboard button and runtime producers. It never
restarts Docker and always restores RTSP in finally.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - environment gate
    raise SystemExit("Install the workspace Playwright Python package before running this E2E") from exc

ROOT = Path(__file__).resolve().parents[3]
CAMERAS = ("car_camera", "face_camera", "safety_camera")


def _wait_for_runtime_mode(
    base: str, target: str, timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    last_state: dict = {"inputs": {}}
    while time.monotonic() < deadline:
        try:
            last_state = _api(base, "/api/runtime/input")
        except requests.RequestException:
            time.sleep(1)
            continue
        inputs = last_state.get("inputs", {})
        if set(inputs) == set(CAMERAS) and all(
            inputs[camera] == target for camera in CAMERAS
        ):
            return last_state
        time.sleep(0.5)
    raise RuntimeError(f"Runtime input did not reach {target}: {last_state}")


def _health() -> dict:
    result = subprocess.run(
        ["docker", "exec", "edge-safety", "sh", "-lc", "cat /tmp/camera-safety-health.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


def _api(base: str, path: str) -> dict:
    response = requests.get(f"{base}{path}", timeout=30)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"items": payload}


def _browser_live_state(page) -> dict[str, dict[str, int | bool | float]]:
    return page.evaluate(
        """(cameraNames) => Object.fromEntries(cameraNames.map((name) => {
            const root = document.querySelector(`[data-camera="${name}"]`);
            const video = root?.querySelector("video");
            return [name, {
                video_present: Boolean(video),
                video_ready: Boolean(video && video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0),
                video_width: video?.videoWidth || 0,
                video_height: video?.videoHeight || 0,
                bbox_count: root?.querySelectorAll("svg rect").length || 0,
            }];
        }))""",
        list(CAMERAS),
    )


def _wait_for_live_gate(
    page, base_url: str, *, bbox: bool, timeout: float
) -> tuple[str, dict[str, object] | None]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runtime = _api(base_url, "/api/runtime/input")
        reason = runtime.get("reason") or ""
        if all(
            runtime.get("inputs", {}).get(camera) == "rtsp" for camera in CAMERAS
        ) and reason.startswith("mock_source_eof:"):
            return "ended", runtime
        states = _browser_live_state(page)
        if all(
            state.get("bbox_count", 0) > 0 if bbox else state.get("video_ready")
            for state in states.values()
        ):
            return "ready", None
        time.sleep(0.5)
    raise TimeoutError(
        f"Timed out waiting for live {'bbox' if bbox else 'video'} state"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8971")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "mock-live-button")
    args = parser.parse_args()
    run_dir = args.output / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema_version": 1,
        "accepted": False,
        "started_at": datetime.now(UTC).isoformat(),
        "gates": {},
        "errors": [],
    }
    runtime_restored = False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_navigation_timeout(args.timeout * 1000)
            page.set_default_timeout(args.timeout * 1000)
            page.goto(args.base_url, wait_until="domcontentloaded")
            page.get_by_role("button", name="Test mock live").wait_for(state="visible")
            notification_before = _api(
                args.base_url, "/api/notifications/providers/status"
            )
            zalo_before = notification_before.get("zalo", {})
            if zalo_before.get("enabled") is not False:
                raise RuntimeError(f"Zalo provider is not disabled: {zalo_before}")
            report["gates"]["zalo_disabled"] = zalo_before
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith("/api/runtime/input/start"),
                timeout=args.timeout * 1000,
            ) as start_response_info:
                page.get_by_role("button", name="Test mock live").click()
            start_response = start_response_info.value
            if start_response.status != 200:
                raise RuntimeError(
                    f"Runtime mock start failed with HTTP {start_response.status}"
                )
            start_payload = start_response.json()
            if set(start_payload.get("inputs", {})) != set(CAMERAS) or any(
                value != "mock" for value in start_payload.get("inputs", {}).values()
            ):
                raise RuntimeError(f"Runtime mock start returned unexpected state: {start_payload}")
            page.wait_for_function(
                """async () => {
                    const r = await fetch('/api/runtime/input');
                    const p = await r.json();
                    return Object.values(p.inputs || {}).length === 3
                        && Object.values(p.inputs || {}).every(v => v === 'mock');
                }""",
                timeout=args.timeout * 1000,
            )
            report["gates"]["frontend_clicked"] = True
            report["gates"]["runtime_mock"] = _api(args.base_url, "/api/runtime/input")

            frame_gate, ended_state = _wait_for_live_gate(
                page, args.base_url, bbox=False, timeout=args.timeout
            )
            if frame_gate == "ended":
                report["gates"]["runtime_auto_stopped"] = ended_state
                raise RuntimeError("A mock video ended before all live frames were ready")
            live_state = _browser_live_state(page)
            report["gates"]["live_video_frames"] = live_state

            bbox_gate, ended_state = _wait_for_live_gate(
                page, args.base_url, bbox=True, timeout=args.timeout
            )
            if bbox_gate == "ended":
                report["gates"]["runtime_auto_stopped"] = ended_state
                raise RuntimeError("A mock video ended before live bbox was ready for all cameras")
            report["gates"]["live_bbox"] = _browser_live_state(page)

            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                runtime_state = _api(args.base_url, "/api/runtime/input")
                if all(
                    runtime_state.get("inputs", {}).get(camera) == "rtsp"
                    for camera in CAMERAS
                ):
                    raise RuntimeError("A mock video ended before Safety produced its event")
                health = _health()
                if (
                    health.get("source_mode") == "mock"
                    and health.get("last_detection_bbox")
                    and health.get("event_create_successes", 0) > 0
                ):
                    report["gates"]["safety_bbox_event"] = health
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Safety did not produce mock source bbox and event")

            auto_stop_deadline = time.monotonic() + min(args.timeout, 60)
            while time.monotonic() < auto_stop_deadline:
                state_after_event = _api(args.base_url, "/api/runtime/input")
                reason = state_after_event.get("reason") or ""
                if all(
                    state_after_event.get("inputs", {}).get(camera) == "rtsp"
                    for camera in CAMERAS
                ) and reason.startswith("mock_source_eof:"):
                    report["gates"]["runtime_auto_stopped"] = state_after_event
                    report["gates"]["runtime_restored"] = True
                    runtime_restored = True
                    break
                time.sleep(0.5)
            else:
                report["gates"]["runtime_auto_stopped"] = False
                with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and response.url.endswith("/api/runtime/input/stop"),
                    timeout=args.timeout * 1000,
                ) as stop_response_info:
                    page.get_by_role("button", name="Stop mock live").click()
                stop_response = stop_response_info.value
                if stop_response.status != 200:
                    raise RuntimeError(
                        f"Runtime restore failed with HTTP {stop_response.status}"
                    )
                report["gates"]["frontend_stop_clicked"] = True
                _wait_for_runtime_mode(args.base_url, "rtsp", 90)
                report["gates"]["runtime_restored"] = True
                runtime_restored = True

            telegram_before = notification_before.get("telegram", {}).get("last_success")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                provider_status = _api(
                    args.base_url, "/api/notifications/providers/status"
                )
                telegram = provider_status.get("telegram", {})
                if (
                    telegram.get("enabled")
                    and telegram.get("configured")
                    and telegram.get("last_success")
                    and telegram.get("last_success") != telegram_before
                ):
                    report["gates"]["telegram_sent"] = telegram
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Telegram did not report a new sent notification after the mock event"
                )
    except Exception as exc:  # noqa: BLE001 - report the complete acceptance failure
        report["errors"].append(str(exc))
    finally:
        if not runtime_restored:
            recovery_request_error = None
            try:
                requests.post(
                    f"{args.base_url}/api/runtime/input/stop", timeout=90
                ).raise_for_status()
            except requests.RequestException as exc:
                recovery_request_error = exc
            try:
                _wait_for_runtime_mode(args.base_url, "rtsp", 90)
                report["gates"]["runtime_restored"] = True
                runtime_restored = True
            except Exception as exc:  # noqa: BLE001
                report["gates"]["runtime_restored"] = False
                if recovery_request_error is not None:
                    report["errors"].append(f"recovery request: {recovery_request_error}")
                report["errors"].append(f"recovery: {exc}")
        else:
            report["gates"]["runtime_restored"] = True
        required_gates = (
            "frontend_clicked",
            "runtime_mock",
            "zalo_disabled",
            "live_video_frames",
            "live_bbox",
            "safety_bbox_event",
            "telegram_sent",
            "runtime_auto_stopped",
            "runtime_restored",
        )
        report["accepted"] = not report["errors"] and all(
            bool(report["gates"].get(gate)) for gate in required_gates
        )
        report["finished_at"] = datetime.now(UTC).isoformat()
        (run_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
