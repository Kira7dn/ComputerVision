from __future__ import annotations

import json
from pathlib import Path

from interfaces import dashboard_api


def test_dashboard_config_is_cached_until_yaml_changes(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "dev.yaml"
    config_path.write_text("profile: dev\n", encoding="utf-8")
    calls = 0

    def load(_path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"profile": "dev", "cameras": []}

    monkeypatch.setattr(dashboard_api, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_api, "load_raw_config", load)
    monkeypatch.setattr(dashboard_api, "CONFIG_CACHE", None)

    assert dashboard_api._raw_config()["profile"] == "dev"
    assert dashboard_api._raw_config()["profile"] == "dev"
    assert calls == 1


def test_event_journal_reads_only_appended_lines(tmp_path) -> None:
    journal = tmp_path / "events.jsonl"
    first = {"record_type": "START", "event_id": "event-1"}
    second = {"record_type": "UPDATE", "event_id": "event-1"}
    journal.write_text(
        "\n".join(json.dumps(item) for item in (first, second)) + "\n",
        encoding="utf-8",
    )
    dashboard_api.JOURNAL_CACHE.clear()

    cursor, lines, event_count = dashboard_api._event_journal_snapshot(
        journal,
        after=0,
    )
    assert cursor == 2
    assert len(lines) == 2
    assert event_count == 1

    third = {"record_type": "START", "event_id": "event-2"}
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(third) + "\n")

    cursor, lines, event_count = dashboard_api._event_journal_snapshot(
        journal,
        after=2,
    )
    assert cursor == 3
    assert [json.loads(line)["event_id"] for _sequence, line in lines] == ["event-2"]
    assert event_count == 2


def test_jetson_gpu_metrics_use_sysfs_when_nvidia_smi_is_not_supported(
    tmp_path, monkeypatch
) -> None:
    gpu = tmp_path / "sys/devices/platform/bus@0/17000000.gpu/load"
    gpu.parent.mkdir(parents=True)
    gpu.write_text("682\n", encoding="ascii")
    model = tmp_path / "proc/device-tree/model"
    model.parent.mkdir(parents=True)
    model.write_text("NVIDIA Jetson Orin Nano\x00", encoding="ascii")
    thermal = tmp_path / "sys/devices/virtual/thermal/thermal_zone1"
    thermal.mkdir(parents=True)
    (thermal / "type").write_text("gpu-thermal\n", encoding="ascii")
    (thermal / "temp").write_text("63156\n", encoding="ascii")

    monkeypatch.setattr(dashboard_api, "JETSON_GPU_LOAD_PATHS", (gpu,))
    monkeypatch.setattr(dashboard_api, "JETSON_MODEL_PATH", model)
    monkeypatch.setattr(dashboard_api, "JETSON_THERMAL_ROOT", thermal.parent)

    assert dashboard_api._read_jetson_gpu() == {
        "available": True,
        "name": "NVIDIA Jetson Orin Nano",
        "utilization_percent": 68.2,
        "memory_used_mb": None,
        "memory_total_mb": None,
        "temperature_c": 63.2,
    }


def test_gpu_metrics_fall_back_when_nvidia_smi_returns_na(monkeypatch) -> None:
    monkeypatch.setattr(
        dashboard_api.subprocess,
        "check_output",
        lambda *args, **kwargs: "Orin (nvgpu), [N/A], [N/A], [N/A], [N/A]\n",
    )
    expected = {"available": True, "name": "Jetson", "utilization_percent": 42.0}
    monkeypatch.setattr(dashboard_api, "_read_jetson_gpu", lambda: expected)

    assert dashboard_api._read_gpu() == expected
