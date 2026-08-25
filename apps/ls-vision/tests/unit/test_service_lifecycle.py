from __future__ import annotations

from dataclasses import dataclass

from ls_vision import service


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

    assert service.main() == 1
    assert runner.terminated is True
