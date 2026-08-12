from pathlib import Path


def test_packaging_builder_has_single_source_and_pinned_transport():
    source = Path("tools/package_recognition_wheels.py").read_text(encoding="utf-8")
    assert "TemporaryDirectory" in source
    assert '"grpcio==1.82.0"' in source
    assert "source_commit" in source
    assert "source_worktree_hash" in source


def test_fault_entrypoint_is_thin_and_shared():
    source = Path("tools/tests/e2e/run_external_recognition_fault_test.py").read_text(encoding="utf-8")
    assert "validate_platform_runtime import main" in source
    assert "--fault-scenario" in source
