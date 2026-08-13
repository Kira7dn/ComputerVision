from __future__ import annotations

from pathlib import Path


def _launcher() -> str:
    return Path("deploy/run.ps1").read_text(encoding="utf-8")


def _tracker_run() -> str:
    return Path("deploy/reference/tracker-run").read_text(encoding="utf-8")


def test_tracker_build_is_owned_by_launcher() -> None:
    launcher = _launcher()
    assert "Dockerfile.tracker" in launcher
    assert "Invoke-BuildStep 'tracker overlay'" in launcher
    assert "tracker_digest = $trackerId" in launcher
    assert "worktree_hash = $worktreeHash" in launcher


def test_tracker_entrypoint_uses_frigate_package_root() -> None:
    tracker_run = _tracker_run()
    assert "cd /opt/frigate" in tracker_run
    assert "python3 -u -m extension.tracker.service.app" in tracker_run


def test_tracker_camera_readiness_precedes_frigate() -> None:
    launcher = _launcher()
    tracker_start = launcher.index("@($trackerNodes.Service)")
    tracker_ready = launcher.index("Wait-TrackerReady $trackerNodes", tracker_start)
    frigate_start = launcher.index("'--no-deps','frigate'", tracker_ready)
    assert tracker_start < tracker_ready < frigate_start


def test_tracker_readiness_treats_startup_connection_errors_as_failed_polls() -> None:
    launcher = _launcher()
    start = launcher.index("function Wait-TrackerReady")
    end = launcher.index("function Ensure-FrigateConfigVolume", start)
    readiness = launcher[start:end]
    assert "$ErrorActionPreference = 'SilentlyContinue'" in readiness
    assert "$ErrorActionPreference = $savedErrorActionPreference" in readiness


def test_tracker_runtime_uses_private_state_and_mtls() -> None:
    launcher = _launcher()
    # Edge mounts its private volume at Frigate's existing media root so the
    # shared recorder/output code is reused without path-specific forks.
    assert ":/media/frigate" in launcher
    assert ":/var/lib/camera-tracker/spool" in launcher
    assert ":/run/tracker-tls:ro" in launcher
    assert "grpc.secure_channel" in launcher
    assert "response.mtls_required" in launcher


def test_tracker_receives_the_same_config_environment_as_frigate() -> None:
    launcher = _launcher()
    tracker = launcher[launcher.index("foreach ($node in $TrackerNodes)") :]
    assert "$lines.Add('    env_file:')" in tracker
    assert "FRIGATE_TELEGRAM_CHAT_ID" in tracker
    assert "FRIGATE_TELEGRAM_BOT_TOKEN" in tracker
    assert "FRIGATE_ZALO_CHAT_ID" in tracker
    assert "FRIGATE_ZALO_BOT_TOKEN" in tracker


def test_each_tracker_runtime_config_contains_only_its_node() -> None:
    launcher = _launcher()
    compiler = Path("frigate/src/extension/topology/compiler.py").read_text(encoding="utf-8")
    topology_launcher = launcher[
        launcher.index("function Initialize-PlatformTopology") : launcher.index(
            "function Get-FirstStream"
        )
    ]
    assert 'edge["tracker"] = {node.node_id:' in compiler
    assert "compile_platform_topology.py" in launcher
    assert "yaml.safe_load" not in topology_launcher
    assert '$effectiveConfig = if (' in launcher
    assert '-v "${effectiveConfig}:/config/config.yml:ro"' in launcher


def test_main_live_streams_proxy_private_edge_go2rtc() -> None:
    launcher = _launcher()
    compiler = Path("frigate/src/extension/topology/compiler.py").read_text(encoding="utf-8")
    assert 'streams[camera] = f"rtsp://{node.service}:8554/{camera}"' in compiler
    assert "if name in wanted" in compiler
    assert "Initialize-PlatformTopology" in launcher
    assert "New-TrackerRuntimeConfigs" not in launcher
