"""Validate and build bounded per-camera passage replay fixtures."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml


def frame_spec(manifest: dict[str, Any], kind: str) -> tuple[int, int, int]:
    """Return the declared replay frame for one pipeline."""
    value = (
        manifest["frame"]
        if kind == "face"
        else manifest[kind].get("frame", manifest["frame"])
    )
    if not isinstance(value, dict):
        raise ValueError(f"{kind} frame must be an object")
    width = int(value.get("width", 0))
    height = int(value.get("height", 0))
    fps = int(value.get("fps", 0))
    if width <= 0 or height <= 0 or fps <= 0 or width % 2 or height % 2:
        raise ValueError(f"{kind} frame must use positive even dimensions and FPS")
    return width, height, fps


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path, root: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise ValueError("manifest schema_version must be 2")
    if not 0 < int(data.get("test_case_limit_seconds", 0)) < 120:
        raise ValueError("test_case_limit_seconds must be under 120")
    frame_spec(data, "face")
    frame_spec(data, "lpr")
    face = data.get("face", {})
    face_source = str(face.get("source", ""))
    if not face_source or not (root / face_source).is_file():
        raise FileNotFoundError(face_source or "face.source")
    if face.get("enrollment"):
        raise ValueError("face fixture must use the configured face library snapshot")
    if face.get("passages") or face.get("close_follow"):
        raise ValueError("face fixture must not define passages or close_follow")
    lpr = data.get("lpr", {})
    lpr_passages = lpr.get("passages", [])
    active_lpr = [p for p in lpr_passages if p.get("valid_passage", True)]
    readable_lpr = [p for p in active_lpr if p.get("readable")]
    if len(active_lpr) < 5 or len(readable_lpr) < 3:
        raise ValueError(
            "LPR requires at least five vehicle passages and three readable passages"
        )
    lpr_seen: set[str] = set()
    expected_plates: set[str] = set()
    for passage in lpr_passages:
        pid = str(passage.get("id", ""))
        if not pid or pid in lpr_seen:
            raise ValueError("all passage IDs must be globally unique")
        lpr_seen.add(pid)
        plate = passage.get("expected_plate")
        if (
            passage.get("valid_passage", True)
            and passage.get("readable")
            and (
                not plate
                or not str(plate).isalnum()
                or str(plate) != str(plate).upper()
            )
        ):
            raise ValueError(
                f"readable LPR passage requires uppercase alphanumeric plate: {pid}"
            )
        if plate:
            normalized = str(plate)
            if normalized in expected_plates:
                raise ValueError(f"LPR expected plates must be unique: {normalized}")
            expected_plates.add(normalized)
        for variant in passage.get("accepted_plates", []):
            if not str(variant).isalnum() or str(variant) != str(variant).upper():
                raise ValueError(
                    f"accepted plate variants must be uppercase alphanumeric: {pid}"
                )
        if not passage.get("readable") and plate is not None:
            raise ValueError(f"unreadable LPR passage cannot have a label: {pid}")
    if not (root / lpr["source"]).is_file():
        raise FileNotFoundError(lpr["source"])
    return data


FACE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def snapshot_face_library(
    config: dict[str, Any], root: Path, runtime_media: Path
) -> dict[str, Any]:
    """Copy a hashed identity-only snapshot without mutating production media."""
    media_value = Path(str(config["runtime"]["media_dir"]))
    media_root = media_value if media_value.is_absolute() else root / media_value
    source = media_root.resolve() / "clips" / "faces"
    if not source.is_dir():
        raise FileNotFoundError(f"face library is missing: {source}")

    destination = runtime_media / "clips" / "faces"
    destination.mkdir(parents=True, exist_ok=True)
    identities: dict[str, dict[str, Any]] = {}
    casefold_names: set[str] = set()
    library_digest = hashlib.sha256()
    image_count = 0
    for identity_dir in sorted(source.iterdir(), key=lambda path: path.name.casefold()):
        if not identity_dir.is_dir() or identity_dir.name.casefold() == "train":
            continue
        folded = identity_dir.name.casefold()
        if folded in casefold_names:
            raise ValueError(
                f"face identity names collide case-insensitively: {identity_dir.name}"
            )
        casefold_names.add(folded)
        files = sorted(
            (
                path
                for path in identity_dir.iterdir()
                if path.is_file() and path.suffix.lower() in FACE_IMAGE_SUFFIXES
            ),
            key=lambda path: path.name,
        )
        if not files:
            continue
        target_dir = destination / identity_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        file_entries = []
        for source_file in files:
            target = target_dir / source_file.name
            shutil.copy2(source_file, target)
            digest = file_hash(target)
            relative = Path(identity_dir.name) / source_file.name
            library_digest.update(relative.as_posix().encode("utf-8"))
            library_digest.update(digest.encode("ascii"))
            file_entries.append(
                {
                    "name": source_file.name,
                    "sha256": digest,
                    "bytes": target.stat().st_size,
                }
            )
            image_count += 1
        identities[identity_dir.name] = {
            "image_count": len(file_entries),
            "files": file_entries,
        }
    if not identities:
        raise ValueError(f"face library has no identity images: {source}")
    return {
        "source": str(source),
        "identity_count": len(identities),
        "image_count": image_count,
        "sha256": library_digest.hexdigest(),
        "identities": identities,
        "train_copied": False,
    }


def lpr_source(
    manifest: dict[str, Any], root: Path
) -> tuple[Path, list[dict[str, Any]]]:
    """Return the authoritative continuous MP4 without transcoding it."""
    source = (root / manifest["lpr"]["source"]).resolve()
    windows = [
        {
            "id": passage["id"],
            "valid_passage": passage.get("valid_passage", True),
        }
        for passage in manifest["lpr"]["passages"]
    ]
    return source, windows


def face_source(
    manifest: dict[str, Any], root: Path
) -> tuple[Path, list[dict[str, Any]]]:
    """Return the authoritative continuous Face MP4 without modification."""
    source = (root / manifest["face"]["source"]).resolve()
    return source, []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("deploy/config.yaml"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tools/fixtures/platform_passage_ground_truth.yaml"),
    )
    parser.add_argument("--output", type=Path, default=Path(".tmp/platform-passage"))
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    root = Path.cwd()
    manifest = load_manifest(args.manifest, root)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace = (
        args.workspace.resolve()
        if args.workspace is not None
        else Path(tempfile.mkdtemp(prefix="camera-platform-runtime-"))
    )
    workspace.mkdir(parents=True, exist_ok=True)
    runtime_media = workspace / "media"
    database_dir = runtime_media / "passage"
    database_dir.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    config = copy.deepcopy(config)
    face_library = snapshot_face_library(config, root, runtime_media)
    face_replay, face_windows = face_source(manifest, root)
    # LPR already has one authoritative source timeline. Do not cut it into
    # passages or insert synthetic frames between physical vehicles.
    lpr_replay, lpr_windows = lpr_source(manifest, root)
    config["runtime"]["media_dir"] = str(runtime_media)
    direct_sources = {"face_camera": str(face_replay), "car_camera": str(lpr_replay)}
    config["runtime"].pop("replay", None)
    config["runtime"]["direct"] = {"sources": direct_sources}
    config.pop("go2rtc", None)
    for camera in ("face_camera", "car_camera"):
        config["cameras"][camera]["ffmpeg"]["inputs"] = [
            {
                "path": f"/runtime-input/{camera}.mp4",
                "input_args": ["-re", "-fflags", "+genpts"],
                "roles": ["detect"],
            },
            {
                "path": f"/runtime-input/{camera}.mp4",
                "input_args": ["-re", "-fflags", "+genpts"],
                "roles": ["record"],
            },
        ]
    # Runtime evidence must exercise the production media path.  Keep the
    # configured record/snapshot features intact and suppress only external
    # notification delivery.
    config["notifications"]["enabled"] = False
    config["database"] = {"path": "/media/frigate/passage/frigate.db"}
    config["cameras"]["face_camera"]["face_recognition"]["min_area"] = 750
    # Match the fixed Face clip's native cadence so short tracks receive enough
    # synchronous recognition observations. Keep the missing-track lifetime at
    # one second so a later entrant cannot inherit an earlier person's raw ID.
    config["cameras"]["face_camera"]["detect"]["fps"] = 15
    config["cameras"]["face_camera"]["detect"]["max_disappeared"] = 15
    # This fixture is a fast highway scene. End unmatched vehicle tracks after
    # one second so a following car cannot inherit a stale five-second lineage.
    config["cameras"]["car_camera"]["detect"]["max_disappeared"] = 5
    config_path = output / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    model_path = root / str(config["runtime"]["model_path"])
    result = {
        "schema_version": 2,
        "builder_version": 11,
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": file_hash(args.manifest),
        "base_config_sha256": file_hash(args.config),
        "model_sha256": file_hash(model_path),
        "face_library_snapshot": face_library,
        "source_sha256": {"face": file_hash(face_replay), "lpr": file_hash(lpr_replay)},
        "replay_frames": {
            kind: dict(zip(("width", "height", "fps"), frame_spec(manifest, kind)))
            for kind in ("face", "lpr")
        },
        "replay_windows": {"face": face_windows, "lpr": lpr_windows},
        "config": str(config_path),
        "config_sha256": file_hash(config_path),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    (output / "fixture.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
