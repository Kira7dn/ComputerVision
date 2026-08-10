"""Validate and build bounded per-camera passage replay fixtures."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import yaml

BLACK_LEAD_SECONDS = 1.5


def frame_spec(manifest: dict[str, Any], kind: str) -> tuple[int, int, int]:
    """Return the declared replay frame for one pipeline."""
    value = manifest["frame"] if kind == "face" else manifest[kind].get(
        "frame", manifest["frame"]
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


def _boxes(value: Any, name: str, width: int, height: int) -> None:
    if not isinstance(value, list) or len(value) != 4 or any(float(v) < 0 for v in value):
        raise ValueError(f"{name} must be a four-number non-negative bbox")
    if float(value[0]) >= float(value[2]) or float(value[1]) >= float(value[3]):
        raise ValueError(f"{name} must have positive width and height")
    if float(value[2]) > width or float(value[3]) > height:
        raise ValueError(f"{name} is outside the {width}x{height} frame")


def load_manifest(path: Path, root: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise ValueError("manifest schema_version must be 2")
    if not 0 < int(data.get("test_case_limit_seconds", 0)) < 120:
        raise ValueError("test_case_limit_seconds must be under 120")
    face_width, face_height, _ = frame_spec(data, "face")
    lpr_width, lpr_height, _ = frame_spec(data, "lpr")
    seen: set[str] = set()
    face = data.get("face", {})
    passages = face.get("passages", [])
    active_passages = [p for p in passages if p.get("valid_passage", True)]
    known_passages = [p for p in active_passages if p.get("expected_identity") not in {None, "unknown"}]
    unknown_passages = [p for p in active_passages if p.get("expected_identity") == "unknown"]
    if len(known_passages) < 2 or len(unknown_passages) < 2:
        raise ValueError("face requires at least two known and two unknown passages")
    for passage in passages:
        pid = str(passage.get("id", ""))
        if not pid or pid in seen:
            raise ValueError("passage IDs must be non-empty and unique")
        seen.add(pid)
        start, end = float(passage.get("start_s", -1)), float(passage.get("end_s", -1))
        if not 0 <= start < end or end - start > 15 or not start <= float(passage.get("face_visible_s", -1)) <= end:
            raise ValueError(f"invalid face passage window: {pid}")
        if passage.get("valid_passage", True) and passage.get("expected_identity") not in {None, "unknown"} and not str(passage.get("expected_identity", "")).strip():
            raise ValueError(f"invalid face identity: {pid}")
        if passage.get("valid_passage", True) and passage.get("bbox") is None:
            raise ValueError(f"active face passage requires bbox: {pid}")
        if passage.get("bbox") is not None:
            _boxes(
                passage.get("bbox"),
                f"face {pid}.bbox",
                face_width,
                face_height,
            )
        if not (root / passage["source"]).is_file():
            raise FileNotFoundError(passage["source"])
    close_follow = face.get("close_follow", [])
    active_ids = {str(p["id"]) for p in active_passages}
    if not close_follow or any(not isinstance(pair, list) or len(pair) != 2 or any(str(pid) not in active_ids for pid in pair) for pair in close_follow):
        raise ValueError("face requires at least one valid close_follow pair")
    lpr = data.get("lpr", {})
    lpr_passages = lpr.get("passages", [])
    active_lpr = [p for p in lpr_passages if p.get("valid_passage", True)]
    readable_lpr = [p for p in active_lpr if p.get("readable")]
    if len(active_lpr) < 5 or len(readable_lpr) < 3:
        raise ValueError("LPR requires at least five vehicle passages and three readable passages")
    lpr_seen: set[str] = set()
    for passage in lpr_passages:
        pid = str(passage.get("id", ""))
        if not pid or pid in seen or pid in lpr_seen:
            raise ValueError("all passage IDs must be globally unique")
        lpr_seen.add(pid)
        start, end = float(passage.get("start_s", -1)), float(passage.get("end_s", -1))
        if not 0 <= start < end <= float(lpr.get("duration_s", 0)):
            raise ValueError(f"invalid LPR passage window: {pid}")
        plate = passage.get("expected_plate")
        if passage.get("valid_passage", True) and passage.get("readable") and (not plate or not str(plate).isalnum() or str(plate) != str(plate).upper()):
            raise ValueError(f"readable LPR passage requires uppercase alphanumeric plate: {pid}")
        for variant in passage.get("accepted_plates", []):
            if not str(variant).isalnum() or str(variant) != str(variant).upper():
                raise ValueError(f"accepted plate variants must be uppercase alphanumeric: {pid}")
        if not passage.get("readable") and plate is not None:
            raise ValueError(f"unreadable LPR passage cannot have a label: {pid}")
        if passage.get("valid_passage", True) and (passage.get("bbox") is None or passage.get("roi") is None):
            raise ValueError(f"active LPR passage requires bbox and roi: {pid}")
        if passage.get("bbox") is not None:
            _boxes(
                passage.get("bbox"),
                f"LPR {pid}.bbox",
                lpr_width,
                lpr_height,
            )
        if passage.get("roi") is not None:
            _boxes(
                passage.get("roi"),
                f"LPR {pid}.roi",
                lpr_width,
                lpr_height,
            )
    if not (root / lpr["source"]).is_file():
        raise FileNotFoundError(lpr["source"])
    simultaneous = False
    for index, left in enumerate(active_lpr):
        for right in active_lpr[index + 1 :]:
            overlaps = max(float(left["start_s"]), float(right["start_s"])) < min(float(left["end_s"]), float(right["end_s"]))
            if overlaps and bbox_iou(left["bbox"], right["bbox"]) < 0.05:
                simultaneous = True
    if not simultaneous:
        raise ValueError("LPR requires two simultaneous vehicles distinguished by bbox")
    return data


def bbox_iou(left: list[float], right: list[float]) -> float:
    ax1, ay1, ax2, ay2 = (float(v) for v in left)
    bx1, by1, bx2, by2 = (float(v) for v in right)
    intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - intersection
    return intersection / union if union else 0.0


def ffmpeg(args: list[str], timeout: int = 30) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args, "-y"], check=True, timeout=timeout)


def write_enrollment_crop(manifest: dict[str, Any], root: Path, output: Path) -> None:
    """Write a detected face crop; the face library must not ingest a full scene."""
    enrollment = manifest["face"]["enrollment"]
    source = root / enrollment["source"]
    capture = cv2.VideoCapture(str(source))
    capture.set(cv2.CAP_PROP_POS_MSEC, float(enrollment["frame_s"]) * 1000)
    ok, frame = capture.read()
    capture.release()
    if not ok or frame is None:
        raise RuntimeError(f"cannot read enrollment frame: {source}")

    matching = next(
        (
            passage
            for passage in manifest["face"]["passages"]
            if passage.get("bbox")
            and passage["source"] == enrollment["source"]
            and float(passage["start_s"]) <= float(enrollment["frame_s"]) <= float(passage["end_s"])
        ),
        None,
    )
    offset_x = offset_y = 0
    person = frame
    if matching is not None:
        x1, y1, x2, y2 = (int(value) for value in matching["bbox"])
        person = frame[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1

    model = root / "frigate" / "config" / "model_cache" / "facedet" / "facedet.onnx"
    if not model.is_file():
        raise FileNotFoundError(model)
    detector = cv2.FaceDetectorYN.create(
        str(model), "", (person.shape[1], person.shape[0]), 0.5, 0.3
    )
    _, faces = detector.detect(person)
    if faces is None or len(faces) == 0:
        raise RuntimeError("no face detected in enrollment frame")
    best = max(faces, key=lambda value: float(value[2] * value[3] * value[-1]))
    x, y, width, height = (float(value) for value in best[:4])
    pad_x, pad_y = width * 0.15, height * 0.15
    left = max(0, int(offset_x + x - pad_x))
    top = max(0, int(offset_y + y - pad_y))
    right = min(frame.shape[1], int(offset_x + x + width + pad_x))
    bottom = min(frame.shape[0], int(offset_y + y + height + pad_y))
    face = frame[top:bottom, left:right]
    if face.size == 0 or not cv2.imwrite(str(output), face):
        raise RuntimeError(f"cannot write enrollment crop: {output}")


def group_composite_passages(
    passages: list[dict[str, Any]], kind: str
) -> list[list[dict[str, Any]]]:
    if kind != "lpr":
        return [[passage] for passage in passages]

    grouped: list[list[dict[str, Any]]] = []
    for passage in passages:
        if grouped and float(passage["start_s"]) < max(
            float(item["end_s"]) for item in grouped[-1]
        ):
            grouped[-1].append(passage)
        else:
            grouped.append([passage])
    return grouped


def make_composite(manifest: dict[str, Any], root: Path, output: Path, kind: str) -> tuple[Path, list[dict[str, Any]]]:
    width, height, fps = frame_spec(manifest, kind)
    # Replay construction is input preparation, not runtime media. Use an
    # OS-temporary input directory so fixture segments never enter the run's
    # report tree and can never be mistaken for runtime evidence.
    media = Path(tempfile.mkdtemp(prefix=f"camera-platform-{kind}-"))
    parts: list[Path] = []
    windows: list[dict[str, Any]] = []
    timeline = BLACK_LEAD_SECONDS
    lead = media / f"{kind}-black-lead.mp4"
    ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}", "-t", str(BLACK_LEAD_SECONDS), "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(lead)])
    parts.append(lead)
    passages = manifest[kind]["passages"]
    grouped = group_composite_passages(passages, kind)
    close_pairs = {tuple(pair) for pair in manifest.get("face", {}).get("close_follow", [])}
    for index, group in enumerate(grouped):
        passage = group[0]
        group_start = min(float(item["start_s"]) for item in group)
        group_end = max(float(item["end_s"]) for item in group)
        if index:
            previous_id = grouped[index - 1][-1]["id"]
            if kind == "face":
                gap_seconds = 0.6 if (previous_id, passage["id"]) in close_pairs else 0.7
            else:
                gap_seconds = 0.85
            gap = media / f"{kind}-gap-{index:02d}.mp4"
            ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}", "-t", str(gap_seconds), "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(gap)])
            parts.append(gap); timeline += gap_seconds
        part = media / f"{kind}-{index:02d}.mp4"
        duration = group_end - group_start
        ffmpeg(["-ss", str(group_start), "-i", str(root / passage["source"] if kind == "face" else root / manifest["lpr"]["source"]), "-t", str(duration), "-an", "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(part)])
        parts.append(part)
        for grouped_passage in group:
            window = {
                "id": grouped_passage["id"],
                "start_s": round(
                    timeline + float(grouped_passage["start_s"]) - group_start, 3
                ),
                "end_s": round(
                    timeline + float(grouped_passage["end_s"]) - group_start, 3
                ),
                "valid_passage": grouped_passage.get("valid_passage", True),
            }
            if kind == "face":
                window["face_visible_s"] = round(timeline + float(grouped_passage["face_visible_s"]) - float(grouped_passage["start_s"]), 3)
            windows.append(window)
        timeline += duration
    concat = media / f"{kind}-concat.txt"
    concat.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts) + "\n", encoding="utf-8")
    result = media / f"{kind}-replay.mp4"
    ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(concat), "-an", "-vf", f"fps={fps}",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0",
        "-x264-params", "repeat-headers=1", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(result),
    ], timeout=60)
    if timeline > 15:
        raise ValueError(f"{kind} composite exceeds 15 seconds: {timeline:.3f}")
    return result, windows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("deploy/config.yaml"))
    parser.add_argument("--manifest", type=Path, default=Path("tools/fixtures/platform_passage_ground_truth.yaml"))
    parser.add_argument("--output", type=Path, default=Path(".tmp/platform-passage"))
    args = parser.parse_args()
    started = time.monotonic()
    root = Path.cwd()
    manifest = load_manifest(args.manifest, root)
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    database_dir = output / "media" / "passage"
    database_dir.mkdir(parents=True, exist_ok=True)
    enrollment = manifest["face"]["enrollment"]
    enrollment_image = output / "media" / "clips" / "faces" / str(enrollment["identity"]) / "enrollment.jpg"
    enrollment_image.parent.mkdir(parents=True, exist_ok=True)
    write_enrollment_crop(manifest, root, enrollment_image)
    face_replay, face_windows = make_composite(manifest, root, output, "face")
    # LPR is a single source timeline; retain its independent passage windows.
    lpr_replay, lpr_windows = make_composite(manifest, root, output, "lpr")
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    config = copy.deepcopy(config)
    config["runtime"]["media_dir"] = str(output / "media")
    config["runtime"]["replay"]["sources"] = {"face_camera": str(face_replay), "car_camera": str(lpr_replay)}
    # Runtime evidence must exercise the production media path.  Keep the
    # configured record/snapshot features intact and suppress only external
    # notification delivery.
    config["notifications"]["enabled"] = False
    config["database"] = {"path": "/media/frigate/passage/frigate.db"}
    config["cameras"]["car_camera"]["detect"]["min_initialized"] = 1
    # The fixture includes a measured 2.58 s source-PTS gap.  Keep the
    # tracker alive for the configured five-second detection boundary so a
    # temporary queue miss cannot retire the physical passage before Event.
    config["cameras"]["car_camera"]["detect"]["max_disappeared"] = 25
    # Composite passages intentionally use hard scene cuts. Do not let the
    # production lightning heuristic suppress every motion region while the
    # short fixture passage is visible.
    config["cameras"]["car_camera"]["motion"]["lightning_threshold"] = 1.0
    config["cameras"]["face_camera"]["face_recognition"]["min_area"] = 750
    config_path = output / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    model_path = root / str(config["runtime"]["model_path"])
    result = {
        "schema_version": 2,
        "builder_version": 9,
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": file_hash(args.manifest),
        "base_config_sha256": file_hash(args.config),
        "model_sha256": file_hash(model_path),
        "enrollment_image": str(enrollment_image),
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
    (output / "fixture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
