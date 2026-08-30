from pathlib import Path


def test_native_jetson_declares_isolated_production_and_development_services() -> None:
    root = Path(__file__).parents[2]
    systemd = root / "deploy/systemd"
    production = (systemd / "ls-vision.service").read_text(encoding="utf-8")
    development = (systemd / "ls-vision-dev.service").read_text(encoding="utf-8")
    ingress = (systemd / "ls-vision-ingress.service").read_text(encoding="utf-8")
    deploy = (root / "deploy/powershell/deploy-jetson-dev.ps1").read_text(encoding="utf-8")

    assert "config/production.yaml" in production
    assert "CAMERA_RUNTIME_ROOT=/opt/ls-vision/data" in production
    assert "CAMERA_MOCK_TIMELINE_ENABLED=1" in production
    assert "CAMERA_DASHBOARD_UNIX_SOCKET=/run/ls-vision/api.sock" in production
    assert "CAMERA_DASHBOARD_PORT=28080" in development
    assert "CAMERA_DASHBOARD_UNIX_SOCKET=/run/ls-vision/api.sock" in development
    assert "CAMERA_INPUT_RTSP_BASE=rtsp://127.0.0.1:28554" in development
    dev_supervisor = (root / "deploy/dev/jetson_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert "application.mock_timeline_runtime" in dev_supervisor
    assert "runner validates and reconciles config changes per camera" in dev_supervisor
    assert "timeline config change requires service restart" in (
        root / "apps/src/runner.py"
    ).read_text(encoding="utf-8")
    production_e2e = (root / "tests/e2e/run_jetson_production_e2e.py").read_text(
        encoding="utf-8"
    )
    assert 'gates["performance_budget"]' in production_e2e
    assert 'gates["analysis_no_backlog"]' in production_e2e
    assert 'gates["runner_config_accepted"]' in production_e2e
    assert "Conflicts=ls-vision.service" not in development
    assert "interfaces.host_ingress" in ingress
    assert "--vision-unix-socket /run/ls-vision/api.sock" in ingress
    assert "127.0.0.1:18080" not in ingress
    assert "127.0.0.1:8000" in ingress
    assert "tbox.service" not in ingress
    assert "release-manifest.json" in deploy
    assert "Rollback is production-only" in deploy
    assert "Join-Path $cameraPath 'apps'" in deploy
    assert "-C $cameraPath config deploy" in deploy
    assert 'mkdir -p "$RELEASE_ROOT/app"' in deploy
    assert 'tar -xzf "$REMOTE_ARCHIVE" -C "$RELEASE_ROOT/app"' in deploy
    assert 'source_unit="$TARGET/app/deploy/systemd/$unit"' in deploy
    assert 'chown -R letron:letron "$REMOTE_ROOT/data"' in deploy
    assert '"$retained" -le 2' in deploy
