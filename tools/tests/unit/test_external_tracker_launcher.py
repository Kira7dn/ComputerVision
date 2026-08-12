from __future__ import annotations

from pathlib import Path


def _launcher() -> str:
    return Path("deploy/run.ps1").read_text(encoding="utf-8")


def test_tracker_build_is_owned_by_launcher() -> None:
    launcher = _launcher()
    assert "Dockerfile.tracker" in launcher
    assert "Invoke-BuildStep 'tracker overlay'" in launcher
    assert "tracker_digest = $trackerId" in launcher
    assert "worktree_hash = $worktreeHash" in launcher


def test_tracker_camera_readiness_precedes_frigate() -> None:
    launcher = _launcher()
    tracker_start = launcher.index("@($trackerNodes.Service)")
    tracker_ready = launcher.index("Wait-TrackerReady $trackerNodes", tracker_start)
    frigate_start = launcher.index("'--no-deps','frigate'", tracker_ready)
    assert tracker_start < tracker_ready < frigate_start


def test_tracker_runtime_uses_private_state_and_mtls() -> None:
    launcher = _launcher()
    assert ":/media/tracker" in launcher
    assert ":/var/lib/camera-tracker/spool" in launcher
    assert ":/run/tracker-tls:ro" in launcher
    assert "grpc.secure_channel" in launcher
    assert "response.mtls_required" in launcher


def test_main_config_removes_edge_go2rtc_streams() -> None:
    launcher = _launcher()
    assert 'if key not in assigned' in launcher
    assert 'if key in wanted' in launcher
    assert "New-TrackerRuntimeConfigs $config $trackerNodes" in launcher
