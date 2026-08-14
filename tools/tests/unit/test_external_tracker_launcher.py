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
    dockerfile = Path("deploy/reference/Dockerfile.tracker").read_text(encoding="utf-8")
    assert "cd /opt/frigate" in tracker_run
    assert "python3 -u -m extension.tracker.app" in tracker_run
    assert 'ENTRYPOINT ["/bin/sh", "/tracker-run"]' in dockerfile


def test_python_service_hot_reload_uses_compose_watch_without_build() -> None:
    launcher = _launcher()
    assert "CAMERA_HOT_RELOAD = '1'" in launcher
    assert ":/opt/frigate/frigate:ro" in launcher
    assert ":/opt/frigate/extension:ro" in launcher
    assert ":/usr/local/go2rtc/create_config.py:ro" in launcher
    assert "$lines.Add('    image: ${FRIGATE_IMAGE}')" in launcher
    assert 'entrypoint: [\"/bin/sh\", \"/tracker-run\"]' in launcher
    assert "$lines.Add('  recognition:')" in launcher
    assert "$lines.Add('    develop:')" in launcher
    assert "$lines.Add('          action: restart')" in launcher
    assert "':/media/frigate/edge-media'" in launcher
    assert "@('watch','--no-up')" in launcher
    assert "Start-DevWatch $prefix" in launcher
    assert "Get-DevWatchServices" in launcher
    dev_start = launcher[launcher.index("    'start' {") : launcher.index("    'dev-restart' {")]
    assert "--no-build" in dev_start


def test_acceptance_script_creates_missing_services_and_waits_for_readiness() -> None:
    launcher = _launcher()
    acceptance = launcher[
        launcher.index("    'acceptance-start'") : launcher.index(
            "    'acceptance-park'"
        )
    ]
    assert "Test-RuntimeDependencies $runtime" in acceptance
    assert "Invoke-Compose $prefix @('config','--quiet')" in acceptance
    assert acceptance.count("@('up','-d','--no-build'") >= 3
    assert "Wait-RecognitionReady" in acceptance
    assert "Wait-TrackerReady" in acceptance
    assert "Get-FrigateInternalStats" in acceptance


def test_go2rtc_dev_overlay_uses_current_config_package() -> None:
    source = Path(
        "frigate/docker/main/rootfs/usr/local/go2rtc/create_config.py"
    ).read_text(encoding="utf-8")
    assert "from frigate.infrastructure.config.env import" in source
    assert "from frigate.config.env import" not in source


def test_tracker_service_readiness_precedes_frigate_without_camera_gate() -> None:
    launcher = _launcher()
    acceptance = launcher.index("    'acceptance-start'")
    tracker_start = launcher.index(
        "Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + @($trackerNodes.Service))",
        acceptance,
    )
    tracker_ready = launcher.index("Wait-TrackerReady $trackerNodes", tracker_start)
    frigate_start = launcher.index(
        "Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','frigate')",
        tracker_ready,
    )
    assert tracker_start < tracker_ready < frigate_start
    assert "-RequireCameras:$false" in launcher[tracker_ready:frigate_start]
    assert "CAMERA_REUSE_SERVICES" not in launcher[acceptance:frigate_start]
    assert "'acceptance-park'" in launcher
    park = launcher[
        launcher.index("    'acceptance-park'") : launcher.index(
            "    'acceptance-fault'"
        )
    ]
    assert "docker stop --time 3 @targets" not in park
    assert "Acceptance runtime is idle and ready" in park
    assert "'replay-' + $_.Name" in park
    assert "state='idle'" in park


def test_development_restart_stops_frigate_before_replacing_tracker() -> None:
    launcher = _launcher()
    restart = launcher[
        launcher.index("    'dev-restart' {") : launcher.index(
            "    'acceptance-start'", launcher.index("    'dev-restart' {")
        )
    ]
    stop_main = restart.index("@('stop','--timeout','10','frigate')")
    recreate_tracker = restart.index("@('up','-d','--no-build','--force-recreate','--no-deps')")
    assert stop_main < recreate_tracker


def test_stop_removes_launcher_owned_containers_left_by_another_profile() -> None:
    launcher = _launcher()
    stop = launcher[launcher.index("    'stop' {") :]
    assert "$_ -eq 'frigate' -or $_ -like 'camera-*'" in stop
    assert "docker rm -f $remaining" in stop
    assert "docker rm -f @remaining" not in stop
    assert stop.count("@('down','--remove-orphans')") == 2


def test_acceptance_timeouts_are_nested_and_launcher_steps_are_durable() -> None:
    launcher = _launcher()
    validator = Path("tools/runtime/validate_platform_runtime.py").read_text(
        encoding="utf-8"
    )
    acceptance = launcher[launcher.index("    'acceptance-start'") :]
    assert "Wait-TrackerReady $trackerNodes 30 -RequireCameras:$false" in acceptance
    assert "timeout=180 if topology" in validator
    assert 'run_deploy(\n            "stop",' not in validator
    assert "Write-LauncherStep 'recognition-readiness' 'starting'" in acceptance
    assert "Write-LauncherStep 'tracker-service-readiness' 'starting'" in acceptance
    assert "Write-LauncherStep 'frigate-create' 'starting'" in acceptance
    assert "$trackerRunning = Test-ContainerRunning $node.Container" in acceptance


def test_acceptance_validates_storage_and_restores_recognition_lifecycle() -> None:
    launcher = _launcher()
    acceptance = launcher[launcher.index("    'acceptance-start'") :]
    restore = launcher[launcher.index("    'acceptance-restore'") :]
    assert "Test-RuntimeStorage $runtime $config" in acceptance
    assert "Test-RuntimeStorage $runtime $config" in restore
    recognition_start = restore.index("'--no-deps','recognition'")
    recognition_ready = restore.index("Wait-RecognitionReady", recognition_start)
    frigate_start = restore.index("'--no-deps','frigate'", recognition_ready)
    assert recognition_start < recognition_ready < frigate_start


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
    assert "grpc.ssl_channel_credentials" in launcher
    assert "root_certificates=" in launcher
    assert "private_key=" in launcher
    assert "certificate_chain=" in launcher
    assert "response['schema_version'] == 1" in launcher


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
    assert "'-v',\"${effectiveConfig}:/config/config.yml:ro\"" in launcher


def test_main_live_streams_proxy_private_edge_go2rtc() -> None:
    launcher = _launcher()
    compiler = Path("frigate/src/extension/topology/compiler.py").read_text(encoding="utf-8")
    assert 'streams[camera] = f"rtsp://{node.service}:8554/{camera}"' in compiler
    assert "if name in wanted" in compiler
    assert "Initialize-PlatformTopology" in launcher
    assert "New-TrackerRuntimeConfigs" not in launcher
