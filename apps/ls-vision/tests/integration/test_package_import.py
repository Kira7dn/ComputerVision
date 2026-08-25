from pathlib import Path


def test_package_layout_and_clean_config_import() -> None:
    from ls_vision.bootstrap.config import load_raw_config

    root = Path(__file__).parents[2]
    config = load_raw_config(root / "config" / "dev.yaml")
    assert config["cameras"]
    assert (root / "src" / "ls_vision" / "runner.py").is_file()
