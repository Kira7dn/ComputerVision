from __future__ import annotations

import ast
from pathlib import Path

import yaml

WORKSPACE_ROOT = Path(__file__).parents[2]
APP_ROOT = WORKSPACE_ROOT / "apps"


def test_workspace_uses_explicit_app_and_service_boundaries() -> None:
    assert APP_ROOT == WORKSPACE_ROOT / "apps"
    assert (WORKSPACE_ROOT / "services" / "camera-server" / "pyproject.toml").is_file()
    assert not (WORKSPACE_ROOT / "app").exists()
    assert not (WORKSPACE_ROOT / "server").exists()
    assert not (WORKSPACE_ROOT / "pyproject.toml").exists()
    assert not (WORKSPACE_ROOT / "requirements.txt").exists()
    assert not (WORKSPACE_ROOT / "uv.lock").exists()


def test_runtime_has_one_package_namespace() -> None:
    source = APP_ROOT / "src"
    assert {path.name for path in source.iterdir() if path.is_dir()} >= {
        "adapters",
        "application",
        "bootstrap",
        "domain",
        "interfaces",
    }
    assert not (source / "ls_vision").exists()
    assert (WORKSPACE_ROOT / "deploy" / "systemd").is_dir()


def test_domain_does_not_import_outer_layers() -> None:
    forbidden = {"adapters", "application", "bootstrap", "interfaces"}
    for path in (APP_ROOT / "src" / "domain").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in imports
            for prefix in forbidden
        ), path


def test_profiles_are_standalone_and_topology_matches() -> None:
    profiles = [
        yaml.safe_load((WORKSPACE_ROOT / "config" / name).read_text(encoding="utf-8"))
        for name in ("dev.yaml", "production.yaml")
    ]
    expected = ["DMS", "camera_front", "camera_back", "camera_left", "camera_right"]
    for profile in profiles:
        assert "extends" not in profile
        assert [camera["id"] for camera in profile["cameras"]] == expected
    assert profiles[0]["profile"] == "dev"
    assert profiles[1]["profile"] == "production"


def test_runtime_sources_have_no_retired_path_identity() -> None:
    retired = ("/opt/" + "camera-safety", "/mnt/d/" + "BusinessAnalyze/Camera")
    for root in (APP_ROOT / "src", WORKSPACE_ROOT / "config"):
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".yaml", ".toml"}:
                continue
            text = path.read_text(encoding="utf-8")
            assert all(value not in text for value in retired), path
