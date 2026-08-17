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
    response = requests.get(f"{base}{path}", timeout=10)
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
                timeout=30000,
            ) as start_response_info:
                page.get_by_role("button", name="Test mock live").click()
            start_response = start_response_info.value
            if start_response.status != 200:
                raise RuntimeError(
                    f"Runtime mock start failed with HTTP {start_response.status}"
                )
            start_payload = start_response.json()
            if any(value != "mock" for value in start_payload.get("inputs", {}).values()):
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

            page.wait_for_function(
                """(cameraNames) => {
                    const state = Object.fromEntries(cameraNames.map((name) => {
                        const root = document.querySelector(`[data-camera="${name}"]`);
                        const video = root?.querySelector("video");
                        return [name, Boolean(video && video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0)];
                    }));
                    return cameraNames.every((name) => state[name]);
                }""",
                arg=list(CAMERAS),
                timeout=args.timeout * 1000,
            )
            live_state = _browser_live_state(page)
            report["gates"]["live_video_frames"] = live_state

            page.wait_for_function(
                """(cameraNames) => cameraNames.every((name) => {
                    const root = document.querySelector(`[data-camera="${name}"]`);
                    return Boolean(root?.querySelector("svg rect"));
                })""",
                arg=list(CAMERAS),
                timeout=args.timeout * 1000,
            )
            report["gates"]["live_bbox"] = _browser_live_state(page)

            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
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
        try:
            requests.post(f"{args.base_url}/api/runtime/input/stop", timeout=30).raise_for_status()
            deadline = time.monotonic() + 45
            recovery_error = None
            while time.monotonic() < deadline:
                try:
                    state = _api(args.base_url, "/api/runtime/input")
                except requests.RequestException as exc:
                    recovery_error = exc
                    time.sleep(1)
                    continue
                if all(value == "rtsp" for value in state.get("inputs", {}).values()):
                    report["gates"]["runtime_restored"] = True
                    break
                time.sleep(0.5)
            else:
                report["gates"]["runtime_restored"] = False
                if recovery_error is not None:
                    report["errors"].append(f"recovery polling: {recovery_error}")
        except Exception as exc:  # noqa: BLE001
            report["gates"]["runtime_restored"] = False
            report["errors"].append(f"recovery: {exc}")
        required_gates = (
            "frontend_clicked",
            "runtime_mock",
            "zalo_disabled",
            "live_video_frames",
            "live_bbox",
            "safety_bbox_event",
            "telegram_sent",
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
