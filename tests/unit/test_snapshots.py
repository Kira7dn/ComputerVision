from __future__ import annotations

import hashlib
import io
import json
import zipfile

from interfaces import dashboard_api


def test_snapshot_bundle_captures_requested_cameras(monkeypatch) -> None:
    frames = {
        "cabin": b"cabin-jpeg",
        "front": b"front-jpeg",
    }
    monkeypatch.setattr(
        dashboard_api,
        "_stream_manifest",
        lambda: {
            "streams": [
                {
                    "camera_id": camera_id,
                    "rtsp_path": f"/{camera_id}",
                    "state": "READY",
                    "published": True,
                }
                for camera_id in frames
            ]
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_capture_rtsp_snapshot",
        lambda _uri, *, quality: frames[_uri.rsplit("/", 1)[-1]],
    )

    bundle, manifest = dashboard_api._snapshot_bundle(
        {"camera_ids": list(frames), "trigger": "request", "quality": 80}
    )

    assert manifest["schema"] == "letron.vision.snapshot-bundle/v1"
    assert manifest["complete"] is True
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "snapshots/cabin.jpg",
            "snapshots/front.jpg",
        }
        archived_manifest = json.loads(archive.read("manifest.json"))
        for camera_id, data in frames.items():
            item = next(item for item in archived_manifest["cameras"] if item["camera_id"] == camera_id)
            assert item["sha256"] == hashlib.sha256(data).hexdigest()
            assert archive.read(item["filename"]) == data
