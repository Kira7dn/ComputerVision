"""Verify selective config reconciliation without disturbing mock synchronization."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / ".tmp" / "ls-vision-dynamic-e2e" / "summary.json"
REMOTE_PYTHON = "/opt/ls-vision/runtime/venv/bin/python3"
REMOTE_SCRIPT = r"""
import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

import yaml

config = Path('/opt/ls-vision-dev/current/app/config/dev.yaml')
status_path = Path('/opt/ls-vision-dev/data/status/runner.json')
timeline_path = Path('/opt/ls-vision-dev/data/status/mock-timeline.json')
original = config.read_bytes()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def wait_generation(minimum, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = read(status_path)
            if int(state.get('config_generation', 0)) >= minimum and not state.get('reload_error'):
                return state
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise TimeoutError(f'runner generation {minimum} not observed')


def wait_rejection(generation, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = read(status_path)
        if int(state.get('config_generation', 0)) == generation and state.get('reload_error'):
            return state
        time.sleep(0.25)
    raise TimeoutError('malformed YAML rejection not observed')


before = read(status_path)
timeline_before = read(timeline_path)
changed = yaml.safe_load(original.decode('utf-8'))
changed['dms']['attention']['interval_ms'] = int(changed['dms']['attention']['interval_ms']) + 10
temporary = config.with_suffix('.yaml.dynamic-e2e.tmp')
temporary.write_text(yaml.safe_dump(changed, sort_keys=False), encoding='utf-8')
os.replace(temporary, config)
result = {'accepted': False}
try:
    active = wait_generation(int(before['config_generation']) + 1)
    timeline_active = read(timeline_path)
    publisher_before = timeline_before['groups']['vehicle_surround']['cameras']['camera_front']['pid']
    publisher_active = timeline_active['groups']['vehicle_surround']['cameras']['camera_front']['pid']
    checks = {
        'only_dms_restarted': active.get('last_restarted_cameras') == ['DMS'],
        'dms_pid_changed': active['cameras']['DMS']['pid'] != before['cameras']['DMS']['pid'],
        'front_pid_preserved': active['cameras']['camera_front']['pid'] == before['cameras']['camera_front']['pid'],
        'timeline_publisher_preserved': publisher_active == publisher_before,
        'timeline_ready': timeline_active.get('ready') is True,
    }
    result.update({
        'accepted': all(checks.values()),
        'checks': checks,
        'generation_before': before['config_generation'],
        'generation_changed': active['config_generation'],
        'dms_pid_before': before['cameras']['DMS']['pid'],
        'dms_pid_changed': active['cameras']['DMS']['pid'],
        'front_pid': active['cameras']['camera_front']['pid'],
        'timeline_publisher_pid': publisher_active,
    })
finally:
    restore = config.with_suffix('.yaml.dynamic-e2e.restore')
    restore.write_bytes(original)
    os.replace(restore, config)
    restored = wait_generation(int(before['config_generation']) + 2)
    result['generation_restored'] = restored['config_generation']
    result['config_restored_exact'] = config.read_bytes() == original
    result['accepted'] = bool(result['accepted'] and result['config_restored_exact'])

malformed_generation = int(restored['config_generation'])
malformed = config.with_suffix('.yaml.dynamic-e2e.malformed')
malformed.write_text('cameras: [', encoding='utf-8')
os.replace(malformed, config)
try:
    rejected = wait_rejection(malformed_generation)
    metrics = json.load(urlopen('http://127.0.0.1:28080/api/metrics', timeout=5))
    active_ids = [item.get('id') for item in rejected.get('active_cameras', [])]
    dashboard_ids = [
        item.get('id')
        for item in (metrics.get('pipeline', {}) or {}).get('camera_details', [])
    ]
    malformed_checks = {
        'generation_preserved': rejected.get('config_generation') == malformed_generation,
        'worker_pids_preserved': all(
            rejected['cameras'][camera]['pid'] == restored['cameras'][camera]['pid']
            for camera in ('DMS', 'camera_front')
        ),
        'active_projection_preserved': active_ids == [
            'DMS', 'camera_front', 'camera_back', 'camera_left', 'camera_right'
        ],
        'dashboard_projection_preserved': dashboard_ids == active_ids,
    }
    result['malformed_yaml_checks'] = malformed_checks
    result['accepted'] = bool(result['accepted'] and all(malformed_checks.values()))
finally:
    final_restore = config.with_suffix('.yaml.dynamic-e2e.final-restore')
    final_restore.write_bytes(original)
    os.replace(final_restore, config)
    final_state = wait_generation(malformed_generation + 1)
    result['final_generation'] = final_state['config_generation']
    result['config_restored_exact'] = config.read_bytes() == original
    result['accepted'] = bool(result['accepted'] and result['config_restored_exact'])
print(json.dumps(result))
"""


def _ssh(alias: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", alias, command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jetson-alias", default="jetson-nano")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    service = _ssh(args.jetson_alias, "systemctl is-active ls-vision-dev.service")
    if service.returncode != 0 or service.stdout.strip() != "active":
        raise RuntimeError("ls-vision-dev.service must be active for dynamic E2E")
    execution = subprocess.run(
        ["ssh", args.jetson_alias, REMOTE_PYTHON, "-"],
        input=REMOTE_SCRIPT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "completed_at": datetime.now(UTC).isoformat(),
        "accepted": False,
    }
    if execution.returncode == 0:
        try:
            payload = json.loads(execution.stdout)
            if isinstance(payload, dict):
                report.update(payload)
        except ValueError as exc:
            report["error"] = f"invalid remote report: {exc}"
    else:
        report["error"] = execution.stderr.strip() or execution.stdout.strip()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report.get("accepted") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
