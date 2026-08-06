"""Extract temporally separated annotation candidates from the camera-1 mock video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIDEO = ROOT / "mock_videos/car-number-plate-video/cam-in/Traffic Control CCTV.mp4"


def split_for_block(block: int) -> str:
    # Whole temporal blocks, rather than individual frames, prevent adjacent
    # frames of one vehicle passage leaking across train/validation/test.
    return ("train", "train", "train", "train", "train", "train", "val", "val", "test", "test")[block % 10]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp/lpr-dataset")
    parser.add_argument("--stride", type=int, default=15, help="sample every N source frames")
    parser.add_argument("--block-frames", type=int, default=300, help="temporal block size")
    parser.add_argument("--max-frames", type=int, default=120)
    args = parser.parse_args()

    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise SystemExit(f"Cannot open video: {args.video}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    records = []
    source_index = 0
    extracted = 0
    while extracted < args.max_frames:
        ok, frame = capture.read()
        if not ok:
            break
        if source_index % args.stride == 0:
            block = source_index // args.block_frames
            split = split_for_block(block)
            target_dir = args.output / "images" / split
            target_dir.mkdir(parents=True, exist_ok=True)
            # 1280px retains useful plate detail while keeping annotation data manageable.
            scale = min(1.0, 1280 / frame.shape[1])
            if scale < 1.0:
                frame = cv2.resize(frame, (round(frame.shape[1] * scale), round(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)
            name = f"frame_{source_index:06d}.jpg"
            target = target_dir / name
            if not cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise SystemExit(f"Cannot write {target}")
            records.append({
                "image": str(target.relative_to(args.output)).replace("\\", "/"),
                "split": split,
                "source_frame": source_index,
                "source_time_seconds": round(source_index / fps, 3),
                "block": block,
                "labels": {"car": [], "license_plate": []},
            })
            extracted += 1
        source_index += 1
    capture.release()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_video": str(args.video.resolve()),
        "source": {"frame_count": frame_count, "fps": fps, "width": source_width, "height": source_height},
        "sampling": {"stride": args.stride, "block_frames": args.block_frames, "max_frames": args.max_frames},
        "annotation_contract": {"classes": ["car", "license_plate"], "format": "YOLO normalized xywh per line"},
        "records": records,
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (args.output / "ANNOTATE.md").write_text(
        "# Camera 1 native LPR annotation set\n\n"
        "Annotate both `car` and `license_plate` in each image. Write YOLO labels beside images under `labels/<split>/` with one line per object: `class_id center_x center_y width height`, normalized to 0..1.\n\n"
        "Class IDs: `car=0`, `license_plate=1`. Keep the split from `manifest.json`; temporal blocks are intentionally not mixed between splits. Prioritize small, blurred, oblique, edge-touching, and low-contrast plates.\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "frames": len(records), "splits": {split: sum(r["split"] == split for r in records) for split in ("train", "val", "test")}}, indent=2))


if __name__ == "__main__":
    main()
