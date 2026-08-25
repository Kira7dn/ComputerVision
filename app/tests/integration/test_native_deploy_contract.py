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
    assert "CAMERA_DASHBOARD_PORT=28080" in development
    assert "CAMERA_INPUT_RTSP_BASE=rtsp://127.0.0.1:28554" in development
    assert "Conflicts=ls-vision.service" not in development
    assert "ls_vision.interfaces.host_ingress" in ingress
    assert "127.0.0.1:18080" in ingress
    assert "127.0.0.1:8000" in ingress
    assert "tbox.service" not in ingress
    assert "release-manifest.json" in deploy
    assert "Rollback is production-only" in deploy
    assert 'source_unit="$TARGET/app/deploy/systemd/$unit"' in deploy
    assert 'chown -R letron:letron "$REMOTE_ROOT/data"' in deploy
    assert '"$retained" -le 2' in deploy
