"""Export human-review frames and a JSON form for passage ground truth."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2
import yaml


def frame_at(source: Path, seconds: float, destination: Path) -> tuple[int, int]:
    capture = cv2.VideoCapture(str(source))
    capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
    ok, frame = capture.read()
    capture.release()
    if not ok or frame is None:
        raise RuntimeError(f"Could not read {source} at {seconds:.2f}s")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), frame):
        raise RuntimeError(f"Could not write {destination}")
    return int(frame.shape[1]), int(frame.shape[0])


def make_sheet(images: list[tuple[str, Path]], destination: Path) -> None:
    tiles: list[Any] = []
    for label, path in images:
        image = cv2.imread(str(path))
        if image is None:
            continue
        image = cv2.resize(image, (480, 270))
        cv2.rectangle(image, (0, 0), (480, 30), (0, 0, 0), -1)
        cv2.putText(image, label, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(image)
    if not tiles:
        raise RuntimeError("No annotation frames were exported")
    while len(tiles) % 2:
        tiles.append(tiles[-1].copy())
    rows = [cv2.hconcat(tiles[index:index + 2]) for index in range(0, len(tiles), 2)]
    sheet = cv2.vconcat(rows)
    destination.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(destination), sheet)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("tools/fixtures/platform_passage_ground_truth.yaml"))
    parser.add_argument("--output", type=Path, default=Path(".tmp/platform-passage/annotation"))
    args = parser.parse_args()
    root = Path.cwd()
    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    face_frame = manifest["frame"]
    lpr_frame = manifest["lpr"].get("frame", face_frame)
    output = args.output.resolve()
    images: list[tuple[str, Path]] = []
    form: dict[str, Any] = {
        "schema_version": 2,
        "instructions": {
            "identity": "Use P1 or unknown for face passages.",
            "plate": "Use uppercase letters/numbers only; use null when unreadable.",
            "bbox": (
                "Use [x1,y1,x2,y2] in each exported frame. "
                f"Face is {face_frame['width']}x{face_frame['height']}; "
                f"LPR is {lpr_frame['width']}x{lpr_frame['height']}. "
                "Leave null if not visible."
            ),
            "do_not_change": "Do not change id or source.",
        },
        "face": [],
        "lpr": [],
    }
    for passage in manifest["face"]["passages"]:
        seconds = (float(passage["start_s"]) + float(passage["end_s"])) / 2
        path = output / "frames" / f"face-{passage['id']}.jpg"
        width, height = frame_at(root / passage["source"], seconds, path)
        images.append((f"FACE {passage['id']} @ {seconds:.2f}s", path))
        form["face"].append({"id": passage["id"], "source": passage["source"], "source_time_s": round(seconds, 3), "image": str(path), "frame": {"width": width, "height": height}, "identity": None, "bbox": None})
    for passage in manifest["lpr"]["passages"]:
        form["lpr"].append(
            {
                "id": passage["id"],
                "source": manifest["lpr"]["source"],
                "readable": passage.get("readable"),
                "expected_plate": passage.get("expected_plate"),
            }
        )
    make_sheet(images, output / "contact-sheet.jpg")
    (output / "annotation_form.json").write_text(json.dumps(form, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "contact_sheet": str(output / 'contact-sheet.jpg'), "form": str(output / 'annotation_form.json'), "frame_count": len(images)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
