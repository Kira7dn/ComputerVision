from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import runner
import yaml

from bootstrap.config import load_raw_config

ROOT = Path(__file__).parents[2]


class FakeProcess:
    next_pid = 100

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        return int(self.returncode or 0)

    def kill(self) -> None:
        self.returncode = -9


def _write_config(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _supervisor(tmp_path: Path, monkeypatch) -> tuple[runner.CameraSupervisor, Path]:
    raw = deepcopy(load_raw_config(ROOT / "config" / "production.yaml"))
    raw["runtime"]["status_directory"] = str(tmp_path / "status")
    raw["evidence"]["directory"] = str(tmp_path / "evidence")
    config_path = tmp_path / "production.yaml"
    _write_config(config_path, raw)
    monkeypatch.setattr(runner, "write_manifest", lambda *args: None)
    monkeypatch.setattr(runner, "validate_config", lambda config, _path: config)
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda command, **kwargs: FakeProcess(command),
    )
    return runner.CameraSupervisor(config_path, raw, "run-test"), config_path


def test_analysis_change_restarts_only_affected_camera(tmp_path: Path, monkeypatch) -> None:
    supervisor, config_path = _supervisor(tmp_path, monkeypatch)
    supervisor.start()
    dms_before = supervisor.workers["DMS"]
    front_before = supervisor.workers["camera_front"]
    candidate = deepcopy(supervisor.raw_config)
    candidate["dms"]["attention"]["interval_ms"] += 10
    _write_config(config_path, candidate)

    supervisor.reload_if_changed()

    assert supervisor.generation == 2
    assert supervisor.last_restarted_cameras == ["DMS"]
    assert dms_before.terminated is True
    assert supervisor.workers["DMS"] is not dms_before
    assert supervisor.workers["camera_front"] is front_before
    assert front_before.terminated is False


def test_invalid_candidate_keeps_all_workers(tmp_path: Path, monkeypatch) -> None:
    supervisor, config_path = _supervisor(tmp_path, monkeypatch)
    supervisor.start()
    workers_before = dict(supervisor.workers)
    candidate = deepcopy(supervisor.raw_config)
    dms = next(item for item in candidate["cameras"] if item["id"] == "DMS")
    dms["functions"]["unknown"] = True
    _write_config(config_path, candidate)

    supervisor.reload_if_changed()

    assert supervisor.generation == 1
    assert supervisor.reload_error
    assert supervisor.workers == workers_before
    assert all(not process.terminated for process in workers_before.values())


def test_malformed_yaml_keeps_all_workers(tmp_path: Path, monkeypatch) -> None:
    supervisor, config_path = _supervisor(tmp_path, monkeypatch)
    supervisor.start()
    workers_before = dict(supervisor.workers)
    config_path.write_text("cameras: [", encoding="utf-8")

    supervisor.reload_if_changed()

    assert supervisor.generation == 1
    assert supervisor.reload_error
    assert supervisor.workers == workers_before
    assert all(not process.terminated for process in workers_before.values())


def test_timeline_candidate_requires_service_restart(tmp_path: Path, monkeypatch) -> None:
    supervisor, config_path = _supervisor(tmp_path, monkeypatch)
    supervisor.start()
    workers_before = dict(supervisor.workers)
    candidate = deepcopy(supervisor.raw_config)
    for camera in candidate["cameras"]:
        source = camera.get("source", {}) or {}
        if source.get("sync_group") == "vehicle_surround":
            source["sync_period_seconds"] += 1.0
    _write_config(config_path, candidate)

    supervisor.reload_if_changed()

    assert supervisor.generation == 1
    assert "timeline config change requires service restart" in str(supervisor.reload_error)
    assert supervisor.workers == workers_before


def test_media_only_change_requires_timeline_service_restart(
    tmp_path: Path, monkeypatch
) -> None:
    supervisor, config_path = _supervisor(tmp_path, monkeypatch)
    supervisor.start()
    workers_before = dict(supervisor.workers)
    candidate = deepcopy(supervisor.raw_config)
    camera_back = next(
        item for item in candidate["cameras"] if item["id"] == "camera_back"
    )
    camera_back["source"]["media_only"] = False
    _write_config(config_path, candidate)

    supervisor.reload_if_changed()

    assert supervisor.generation == 1
    assert "timeline config change requires service restart" in str(
        supervisor.reload_error
    )
    assert supervisor.workers == workers_before


def test_runner_status_exposes_plan_generation(tmp_path: Path, monkeypatch) -> None:
    supervisor, _config_path = _supervisor(tmp_path, monkeypatch)
    supervisor.start()

    status = yaml.safe_load(supervisor.status_path.read_text(encoding="utf-8"))

    assert status["config_generation"] == 1
    assert status["cameras"]["DMS"]["enabled_functions"] == ["dms"]
    assert status["cameras"]["DMS"]["config_generation"] == 1
    assert status["cameras"]["camera_back"]["media_only"] is True
    assert status["cameras"]["camera_back"]["pid"] is None
    assert [item["id"] for item in status["active_cameras"]] == [
        "DMS",
        "camera_front",
        "camera_back",
        "camera_left",
        "camera_right",
    ]
