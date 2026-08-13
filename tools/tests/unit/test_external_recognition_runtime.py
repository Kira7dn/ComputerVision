from __future__ import annotations

from pathlib import Path

from extension.topology.fingerprint import canonical_json, fingerprint

import tools.runtime.validate_platform_runtime as runtime


def test_topology_configuration_has_one_source_of_truth(monkeypatch, tmp_path):
    created = []
    monkeypatch.setattr(
        runtime, "create_recognition_tls", lambda path: created.append(path)
    )
    config = {}
    tls = runtime.configure_recognition_topology(config, "recognition", tmp_path)
    assert tls == tmp_path / "recognition-tls"
    assert created == [tls]
    assert config["recognition"] == {
        "runtime": "external",
        "endpoint": "recognition:50051",
        "deadline": 5,
        "job_deadline": 30,
        "observation_capacity": 128,
        "control_capacity": 64,
        "outcome_capacity": 128,
        "shutdown_drain": 10,
        "tls": {
            "ca": "/run/recognition-tls/ca.crt",
            "certificate": "/run/recognition-tls/client.crt",
            "key": "/run/recognition-tls/client.key",
            "server_name": "recognition",
        },
    }

    assert runtime.configure_recognition_topology(config, "local", tmp_path) is None
    assert config["recognition"] == {"runtime": "local"}


def test_external_wrapper_reuses_runtime_validator():
    wrapper = Path(
        "tools/tests/e2e/run_external_recognition_runtime_test.py"
    ).read_text(encoding="utf-8")
    assert 'main(["--topology", "recognition"])' in wrapper
    assert "validate_platform_runtime import main" in wrapper


def test_deployment_starts_service_before_frigate():
    deploy = Path("deploy/run.ps1").read_text(encoding="utf-8")
    recognition_start = deploy.index("'--no-deps','recognition'")
    health_wait = deploy.index("Wait-RecognitionReady", recognition_start)
    frigate_start = deploy.index("'--no-deps','frigate'", health_wait)
    assert recognition_start < health_wait < frigate_start
    assert "--profile','external-recognition'" in deploy


def test_recognition_service_uses_an_in_memory_noncanonical_database() -> None:
    source = Path("frigate/src/extension/recognition/app.py").read_text(
        encoding="utf-8"
    )
    assert 'config.database.path = ":memory:"' in source


def test_restore_switches_from_run_scoped_tls_before_launcher_call() -> None:
    validator = Path("tools/runtime/validate_platform_runtime.py").read_text(
        encoding="utf-8"
    )
    restore = validator[validator.index("restore_ok = not runtime_started") :]
    reset = restore.index('os.environ.pop("RECOGNITION_TLS_DIR", None)')
    call = restore.index('run_deploy("acceptance-restore"', reset)
    assert reset < call


def test_replay_topology_is_ready_before_frigate_start():
    deploy = Path("deploy/run.ps1").read_text(encoding="utf-8")
    replay_ready = deploy.index("Wait-ReplayReady $replaySources")
    frigate_start = deploy.index("'--no-deps','frigate'", replay_ready)
    assert replay_ready < frigate_start
    assert "camera-mediamtx running=" in deploy


def test_runtime_fingerprint_is_deterministic_and_utf8():
    first = canonical_json({"z": "á", "a": 1})
    second = canonical_json({"a": 1, "z": "á"})
    assert first == second
    assert fingerprint(first) == fingerprint(second)


def test_tracker_readiness_requires_all_expected_cameras(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(
        '{"tracker_nodes":[{"health":{"ready":true,"degraded":false},'
        '"cameras":[{"camera_id":"face_camera","ready":true},'
        '{"camera_id":"car_camera","ready":true}]}]}',
        encoding="utf-8",
    )
    result = runtime.wait_tracker_ready(
        state, {"face_camera", "car_camera"}, timeout=0.1
    )
    assert result["tracker_nodes"][0]["health"]["ready"] is True
