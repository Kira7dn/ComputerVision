from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import service


@dataclass
class FakeProcess:
    return_code: int | None
    terminated: bool = False
    killed: bool = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        if self.return_code is None:
            self.return_code = -15

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        return int(self.return_code or 0)

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9


def test_service_exits_when_any_critical_child_fails(monkeypatch) -> None:
    dashboard = FakeProcess(None)
    runner = FakeProcess(7)
    created = iter((dashboard, runner))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *args, **kwargs: next(created))
    monkeypatch.setattr(service.signal, "signal", lambda *args: None)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv("CAMERA_MOCK_MEDIA_ROOT", raising=False)
    monkeypatch.setenv("CAMERA_MOCK_TIMELINE_ENABLED", "0")

    assert service.main() == 7
    assert dashboard.terminated is True


def test_clean_child_exit_is_still_a_service_failure(monkeypatch) -> None:
    dashboard = FakeProcess(0)
    runner = FakeProcess(None)
    created = iter((dashboard, runner))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *args, **kwargs: next(created))
    monkeypatch.setattr(service.signal, "signal", lambda *args: None)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv("CAMERA_MOCK_MEDIA_ROOT", raising=False)
    monkeypatch.setenv("CAMERA_MOCK_TIMELINE_ENABLED", "0")

    assert service.main() == 1
    assert runner.terminated is True


def test_mock_media_exit_remains_a_service_failure(monkeypatch) -> None:
    dashboard = FakeProcess(None)
    mock_media = FakeProcess(6)
    runner = FakeProcess(None)
    created = iter((dashboard, mock_media, runner))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *args, **kwargs: next(created))
    monkeypatch.setattr(service.signal, "signal", lambda *args: None)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("CAMERA_MOCK_MEDIA_ROOT", "C:/mock-media")
    monkeypatch.setenv("CAMERA_MOCK_TIMELINE_ENABLED", "0")

    assert service.main() == 6
    assert dashboard.terminated is True
    assert runner.terminated is True


def test_timeline_failure_restarts_only_timeline(monkeypatch) -> None:
    dashboard = FakeProcess(None)
    initial_timeline = FakeProcess(9)

    class DelayedFailure(FakeProcess):
        polls: int = 0

        def poll(self) -> int | None:
            self.polls += 1
            return None if self.polls == 1 else 7

    runner = DelayedFailure(None)
    replacement_timeline = FakeProcess(None)
    created = iter((dashboard, initial_timeline, runner, replacement_timeline))
    commands: list[list[str]] = []

    def popen(command, *args, **kwargs):
        commands.append(command)
        return next(created)

    monkeypatch.setattr(service.subprocess, "Popen", popen)
    monkeypatch.setattr(service, "_wait_for_timeline", lambda *args: None)
    monkeypatch.setattr(service.signal, "signal", lambda *args: None)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv("CAMERA_MOCK_MEDIA_ROOT", raising=False)
    monkeypatch.setenv("CAMERA_MOCK_TIMELINE_ENABLED", "1")

    assert service.main() == 7
    assert [command[2] for command in commands if len(command) > 2 and command[1] == "-m"] == [
        "interfaces.dashboard_api",
        "application.mock_timeline_runtime",
        "runner",
        "application.mock_timeline_runtime",
    ]
    assert initial_timeline.terminated is False
    assert replacement_timeline.terminated is True


def test_timeline_ready_requires_fresh_atomic_status(tmp_path: Path) -> None:
    path = tmp_path / "mock-timeline.json"
    path.write_text('{"ready":true,"updated_at":0}', encoding="utf-8")
    assert service._timeline_ready(path) is False

    path.write_text(
        f'{{"ready":true,"updated_at":{service.time.time()}}}',
        encoding="utf-8",
    )
    assert service._timeline_ready(path) is True
