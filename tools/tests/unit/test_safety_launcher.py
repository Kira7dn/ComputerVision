from pathlib import Path

import pytest
import yaml

from tools.fixtures.prepare_passage_fixture import configure_notifications


def test_safety_launcher_is_optional_and_has_explicit_profile() -> None:
    launcher = Path("deploy/run.ps1").read_text(encoding="utf-8")
    assert "[string]$SafetyConfigFile = ''" in launcher
    assert "external-safety" in launcher
    assert "Wait-SafetyReady" in launcher
    assert "Test-SafetyConfig $config" in launcher


def test_safety_compose_mounts_config_and_model_read_only() -> None:
    compose = Path("deploy/reference/docker-compose.yml").read_text(encoding="utf-8")
    assert "profiles: [\"external-safety\"]" in compose
    assert "SAFETY_CONFIG_FILE" in compose and ":/config/safety.yaml:ro" in compose
    assert "SAFETY_MODEL_PATH" in compose and ":/models/smoking/best.onnx:ro" in compose


def test_safety_replay_uses_real_fixture_and_no_loop() -> None:
    config = Path("deploy/config.safety-replay-smoker.yaml").read_text(encoding="utf-8")
    assert "bucket11.mp4" in config
    assert "loop: false" in config
    assert "detect:\n      enabled: false" in config


@pytest.mark.parametrize(
    "config_path",
    [
        Path("deploy/config.yaml"),
        Path("deploy/config.safety-replay-smoker.yaml"),
        Path("deploy/config.safety-integration.yaml"),
    ],
)
def test_safety_notification_rule_is_present_in_effective_configs(
    config_path: Path,
) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    notifications = config["notifications"]
    rules = {rule["id"]: rule for rule in notifications["rules"]}

    assert notifications["enabled"] is True
    assert rules["smoking_alert"] == {
        "id": "smoking_alert",
        "name": "Smoking alert",
        "enabled": True,
        "event": "alert",
        "filters": {
            "cameras": ["safety_camera"],
            "labels": ["smoking"],
            "zones": [],
            "identities": [],
            "trigger_names": [],
            "conditions": [],
        },
        "destinations": {
            "webpush": True,
            "telegram": ["security_team"],
            "zalo": ["operators"],
        },
        "cooldown": 60,
    }


def test_notification_e2e_switch_enables_every_rule_without_changing_credentials() -> None:
    config = {
        "notifications": {
            "enabled": False,
            "rules": [
                {"id": "car_alert", "enabled": False},
                {"id": "face_recognition", "enabled": False},
                {"id": "smoking_alert", "enabled": True},
            ],
            "channels": {
                "telegram": {"recipients": [{"chat_id": "{TELEGRAM_CHAT_ID}"}]}
            },
        }
    }

    result = configure_notifications(config, enabled=True)

    assert result == {
        "enabled": True,
        "rules": ["car_alert", "face_recognition", "smoking_alert"],
    }
    assert config["notifications"]["channels"]["telegram"]["recipients"][0][
        "chat_id"
    ] == "{TELEGRAM_CHAT_ID}"


def test_notification_e2e_can_select_one_rule_per_expected_event_class() -> None:
    config = {
        "notifications": {
            "enabled": False,
            "rules": [
                {"id": "car_alert", "enabled": False},
                {"id": "car_license_plate", "enabled": False},
                {"id": "face_recognition", "enabled": False},
                {"id": "smoking_alert", "enabled": False},
            ],
        }
    }

    result = configure_notifications(
        config,
        enabled=True,
        rule_ids=["car_alert", "face_recognition", "smoking_alert"],
    )

    assert result["rules"] == ["car_alert", "face_recognition", "smoking_alert"]
