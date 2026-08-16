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
SAFETY_FIXTURE_SOURCE = Path(
    "assets/fixtures/mock_videos/smoker/samples/part1/bucket11.mp4"
)


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


def add_safety_source(
    config: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Inject the Safety mock source only into an isolated E2E config."""
    config.setdefault("runtime", {})
    source = (root / SAFETY_FIXTURE_SOURCE).resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))

    config.setdefault("go2rtc", {}).setdefault("streams", {})[
        "safety_camera"
    ] = ["rtsp://mediamtx:18554/safety_camera"]
    config["runtime"]["replay"] = {
        # The combined E2E launcher starts this publisher only after the
        # Safety control plane is warm. A single pass preserves the source
        # timeline, so a smoking detection in the first seconds stays in the
        # first seconds of the acceptance run.
        "loop": False,
        "sources": {"safety_camera": str(source)},
    }
    safety_camera = (config.get("cameras") or {}).get("safety_camera")
    if not isinstance(safety_camera, dict):
        raise ValueError("config.yaml must define cameras.safety_camera")
    safety_camera["enabled"] = True
    # Safety owns inference and evidence for this lane. Frigate must not run
    # its native detector or capture fallback media for this camera.
    config["cameras"]["safety_camera"].setdefault("detect", {})["enabled"] = False
    return {
        "source": str(source),
        "source_sha256": file_hash(source),
        "config": str((root / "deploy" / "config.yaml").resolve()),
        "config_sha256": file_hash(root / "deploy" / "config.yaml"),
    }


def face_source(
    manifest: dict[str, Any], root: Path
) -> tuple[Path, list[dict[str, Any]]]:
    """Return the authoritative continuous Face MP4 without modification."""
    source = (root / manifest["face"]["source"]).resolve()
    return source, []


def configure_notifications(
    config: dict[str, Any],
    enabled: bool,
    rule_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Set the run-scoped notification policy without exposing credentials."""
    notifications = config.get("notifications")
    if not isinstance(notifications, dict):
        raise ValueError("Passage config must define notifications")
    notifications["enabled"] = enabled
    if enabled:
        selected_rules = set(rule_ids) if rule_ids is not None else None
        for rule in notifications.get("rules", []):
            if isinstance(rule, dict):
                rule["enabled"] = (
                    selected_rules is None or rule.get("id") in selected_rules
                )
    return {
        "enabled": bool(notifications.get("enabled")),
        "rules": [
            str(rule.get("id"))
            for rule in notifications.get("rules", [])
            if isinstance(rule, dict) and rule.get("enabled")
        ],
    }


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
    parser.add_argument(
        "--include-safety",
        action="store_true",
        help="Add the real smoking replay and Safety camera to the fixture.",
    )
    parser.add_argument(
        "--enable-notifications",
        action="store_true",
        help="Enable every configured notification rule for this run.",
    )
    parser.add_argument(
        "--notification-rules",
        nargs="+",
        help="Optional notification rule IDs to enable for this run.",
    )
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
    if args.include_safety:
        config["runtime"]["replay"] = {
            "loop": False,
            "sources": {"safety_camera": str(SAFETY_FIXTURE_SOURCE)},
        }
        safety = add_safety_source(config, root)
    else:
        config["cameras"]["safety_camera"]["enabled"] = False
        safety = None
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
    # Runtime evidence must exercise the production media path. Notifications
    # remain disabled by default because the ordinary acceptance run must not
    # contact external providers. The notification E2E opts in explicitly.
    notification_runtime = configure_notifications(
        config,
        enabled=args.enable_notifications,
        rule_ids=args.notification_rules,
    )
    config["database"] = {"path": "/media/frigate/passage/frigate.infrastructure.db"}
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
        "notifications": notification_runtime,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if safety is not None:
        result["safety"] = safety
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
