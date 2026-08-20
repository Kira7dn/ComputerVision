from pathlib import Path

import yaml


def test_compose_declares_current_services_and_linux_volumes() -> None:
    root = Path(__file__).parents[2]
    compose = yaml.safe_load((root / "deploy/docker/compose.yaml").read_text(encoding="utf-8"))
    assert compose["name"] == "ls-vision"
    assert set(compose["services"]) == {"ls-vision", "mediamtx"}
    assert compose["services"]["ls-vision"]["restart"] == "unless-stopped"
    assert compose["services"]["ls-vision"]["image"].startswith("ls-vision:")
    assert compose["services"]["ls-vision"]["deploy"]["resources"]["reservations"]["devices"]
    assert {"camera_evidence", "camera_state", "camera_queue", "camera_logs"} <= set(compose["volumes"])
