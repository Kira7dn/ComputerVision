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
            page.goto(args.base_url, wait_until="networkidle")
            page.get_by_role("button", name="Test mock live").click()
            page.wait_for_function(
                """async () => {
                    const r = await fetch('/api/runtime/input');
                    const p = await r.json();
                    return Object.values(p.inputs || {}).every(v => v === 'mock');
                }""",
                timeout=args.timeout * 1000,
            )
            report["gates"]["frontend_clicked"] = True
            report["gates"]["runtime_mock"] = _api(args.base_url, "/api/runtime/input")

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

            provider_status = _api(args.base_url, "/api/notifications/providers/status")
            report["gates"]["notification_providers"] = provider_status
            report["gates"]["notification_configured"] = all(
                provider_status.get(provider, {}).get("enabled", False)
                for provider in ("telegram", "zalo")
                if provider in provider_status
            )
            browser.close()
    except Exception as exc:  # noqa: BLE001 - report the complete acceptance failure
        report["errors"].append(str(exc))
    finally:
        try:
            requests.post(f"{args.base_url}/api/runtime/input/stop", timeout=10).raise_for_status()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = _api(args.base_url, "/api/runtime/input")
                if all(value == "rtsp" for value in state.get("inputs", {}).values()):
                    report["gates"]["runtime_restored"] = True
                    break
                time.sleep(0.5)
            else:
                report["gates"]["runtime_restored"] = False
        except Exception as exc:  # noqa: BLE001
            report["gates"]["runtime_restored"] = False
            report["errors"].append(f"recovery: {exc}")
        report["accepted"] = not report["errors"] and bool(report["gates"].get("runtime_restored"))
        report["finished_at"] = datetime.now(UTC).isoformat()
        (run_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
