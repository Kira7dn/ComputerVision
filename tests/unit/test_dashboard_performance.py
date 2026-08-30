from __future__ import annotations

import json
from pathlib import Path

import yaml

from interfaces import dashboard_api


def test_metrics_snapshot_refreshes_with_sequence_at_one_hz(monkeypatch) -> None:
    now = 100.0
    collections = 0

    def monotonic() -> float:
        return now

    def collect(_stream_host: str) -> dict[str, object]:
        nonlocal collections
        collections += 1
        return {"timestamp": float(collections), "pipeline": {"camera_details": []}}

    monkeypatch.setattr(dashboard_api.time, "monotonic", monotonic)
    monkeypatch.setattr(dashboard_api, "_collect_metrics", collect)
    monkeypatch.setattr(dashboard_api, "METRICS_SEQUENCE", 0)
    dashboard_api.METRICS_CACHE.clear()

    first = dashboard_api.collect_metrics("127.0.0.1")
    now += 0.5
    cached = dashboard_api.collect_metrics("127.0.0.1")
    now += 0.31
    second = dashboard_api.collect_metrics("127.0.0.1")

    assert cached is first
    assert collections == 2
    assert first["contract_version"] == 1
    assert first["source_epoch"] == dashboard_api.METRICS_SOURCE_EPOCH
    assert first["sequence"] == 1
    assert second["sequence"] == 2


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


def test_dashboard_keeps_last_valid_config_when_yaml_is_malformed(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "dev.yaml"
    config_path.write_text("profile: dev\n", encoding="utf-8")
    malformed = False

    def load(_path: Path) -> dict[str, object]:
        if malformed:
            raise yaml.YAMLError("malformed")
        return {"profile": "dev", "cameras": []}

    monkeypatch.setattr(dashboard_api, "CONFIG_PATH", config_path)
    monkeypatch.setattr(dashboard_api, "load_raw_config", load)
    monkeypatch.setattr(dashboard_api, "CONFIG_CACHE", None)
    assert dashboard_api._raw_config()["profile"] == "dev"

    malformed = True
    config_path.write_text("cameras: [", encoding="utf-8")

    assert dashboard_api._raw_config()["profile"] == "dev"


def test_active_evidence_run_uses_runner_run_id_not_directory_cache(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "evidence"
    old_run = root / "snapshots-acceptance-old"
    active_run = root / "snapshots-acceptance-active"
    old_run.mkdir(parents=True)
    active_run.mkdir(parents=True)

    monkeypatch.setattr(
        dashboard_api,
        "_active_evidence_location",
        lambda: (root, "snapshots-acceptance"),
    )
    monkeypatch.setattr(
        dashboard_api,
        "_runner_status",
        lambda: {"fresh": True, "run_id": "active"},
    )

    assert dashboard_api._active_evidence_run() == active_run


def test_evidence_metrics_degrade_cleanly_before_index_exists(
    tmp_path, monkeypatch
) -> None:
    active_run = tmp_path / "snapshots-acceptance-active"
    active_run.mkdir()
    monkeypatch.setattr(dashboard_api, "_active_evidence_run", lambda: active_run)
    monkeypatch.setattr(
        dashboard_api,
        "_active_evidence_location",
        lambda: (tmp_path, "snapshots-acceptance"),
    )

    assert dashboard_api._evidence_metrics() == {
        "available": False,
        "run_id": None,
        "event_count": 0,
    }
    assert dashboard_api._event_feed() == {"events": []}


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


def test_mock_timeline_status_fails_closed_when_stale(tmp_path, monkeypatch) -> None:
    status = tmp_path / "mock-timeline.json"
    monkeypatch.setattr(
        dashboard_api,
        "_raw_config",
        lambda: {"runtime": {"status_directory": str(tmp_path)}},
    )
    status.write_text('{"ready":true,"updated_at":0,"groups":{}}', encoding="utf-8")

    assert dashboard_api._mock_timeline_status()["ready"] is False

    status.write_text(
        f'{{"ready":true,"updated_at":{dashboard_api.time.time()},"groups":{{}}}}',
        encoding="utf-8",
    )
    payload = dashboard_api._mock_timeline_status()
    assert payload["ready"] is True
    assert payload["fresh"] is True


def test_live_metadata_merges_packet_publisher_timing_samples(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(dashboard_api, "_status_directory", lambda: tmp_path)
    monkeypatch.setattr(
        dashboard_api,
        "_camera_definitions",
        lambda: [{"id": "camera_front"}],
    )
    (tmp_path / "camera_front.metadata.json").write_text(
        json.dumps({"camera": "camera_front", "frame_timing_samples": []}),
        encoding="utf-8",
    )
    sample = {
        "rtp_timestamp": 9000,
        "capture_timestamp": 100.0,
        "output_timestamp": 100.01,
        "output_pts_ns": 100_000_000,
    }
    monkeypatch.setattr(
        dashboard_api,
        "_mock_timeline_status",
        lambda: {
            "ready": True,
            "groups": {
                "vehicle_surround": {
                    "cameras": {
                        "camera_front": {"frame_timing_samples": [sample]}
                    }
                }
            },
        },
    )

    payload = dashboard_api._live_metadata()

    assert payload["cameras"]["camera_front"]["frame_timing_samples"] == [sample]


def test_runner_status_exposes_fresh_generation(tmp_path, monkeypatch) -> None:
    status = tmp_path / "runner.json"
    monkeypatch.setattr(
        dashboard_api,
        "_raw_config",
        lambda: {"runtime": {"status_directory": str(tmp_path)}},
    )
    status.write_text(
        json.dumps(
            {
                "updated_at": dashboard_api.time.time(),
                "config_generation": 3,
                "last_restarted_cameras": ["DMS"],
            }
        ),
        encoding="utf-8",
    )

    payload = dashboard_api._runner_status()

    assert payload["fresh"] is True
    assert payload["config_generation"] == 3
    assert payload["last_restarted_cameras"] == ["DMS"]


def test_camera_definitions_use_runner_active_config_when_candidate_is_invalid(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        dashboard_api,
        "_runner_status",
        lambda: {
            "fresh": True,
            "active_cameras": [
                {
                    "id": "DMS",
                    "display_name": "Driver",
                    "source": "rtsp://camera/source",
                    "source_type": "rtsp",
                    "media_only": False,
                    "output": "rtsp://127.0.0.1:8554/dms",
                    "functions": {"dms": True},
                }
            ],
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_raw_config",
        lambda: (_ for _ in ()).throw(AssertionError("candidate YAML must not be read")),
    )

    definitions = dashboard_api._camera_definitions("vision.local")

    assert definitions[0]["id"] == "DMS"
    assert definitions[0]["webrtc_url"].endswith("/dms/whep")


def test_stream_manifest_has_six_stable_cameras_and_fails_closed(monkeypatch) -> None:
    ready_paths = {"/camera_front", "/camera_back"}
    monkeypatch.setattr(
        dashboard_api,
        "_runner_status",
        lambda: {"config_generation": 7},
    )
    monkeypatch.setattr(
        dashboard_api,
        "_mock_timeline_status",
        lambda: {
            "ready": True,
            "groups": {"vehicle_surround": {"locked": True}},
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_probe_rtsp_path",
        lambda path: {
            "published": path in ready_paths,
            "codec": "h264" if path in ready_paths else None,
        },
    )

    manifest = dashboard_api._stream_manifest("vision.local")
    streams = manifest["streams"]

    assert manifest["schema"] == "letron.vision.stream-manifest/v1"
    assert manifest["generation"] == 7
    assert manifest["media_base"]["lan_rtsp"] == "rtsp://vision.local:8554"
    assert [item["camera_id"] for item in streams] == [
        "cabin",
        "front",
        "back",
        "left",
        "right",
        "cargo",
    ]
    assert [item["state"] for item in streams] == [
        "OFFLINE",
        "READY",
        "READY",
        "OFFLINE",
        "OFFLINE",
        "READY",
    ]
    assert streams[0]["role"] == "vision_processed"
    assert streams[1]["role"] == "vision_processed"
    assert streams[1]["rtsp_path"] == "/camera_front"
    cargo = streams[-1]
    assert cargo["role"] == "media_only_passthrough"
    assert cargo["rtsp_path"] == "/camera_back"
    assert cargo["sync_group"] == "vehicle_surround"
    assert cargo["source_camera_id"] == "back"
    assert cargo["alias_of"] == "back"


def test_stream_manifest_degrades_surround_stream_when_timeline_is_unlocked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(dashboard_api, "_runner_status", lambda: {})
    monkeypatch.setattr(
        dashboard_api,
        "_mock_timeline_status",
        lambda: {
            "ready": True,
            "groups": {"vehicle_surround": {"locked": False}},
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_probe_rtsp_path",
        lambda _path: {"published": True, "codec": "h264"},
    )

    streams = dashboard_api._stream_manifest()["streams"]
    by_id = {item["camera_id"]: item for item in streams}

    assert by_id["front"]["state"] == "DEGRADED"
    assert by_id["back"]["state"] == "DEGRADED"
    assert by_id["cargo"]["state"] == "DEGRADED"
    assert by_id["cabin"]["state"] == "READY"
