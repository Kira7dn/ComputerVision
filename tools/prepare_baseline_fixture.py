#!/usr/bin/env python3
"""Prepare the isolated, deterministic two-camera baseline replay fixture."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path, root: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    if not 0 < int(data.get("test_case_limit_seconds", 0)) < 120:
        raise ValueError("test_case_limit_seconds must be under 120")

    case_ids: set[str] = set()
    for case in data.get("face", {}).get("cases", []):
        case_id = str(case.get("id", ""))
        if not case_id or case_id in case_ids:
            raise ValueError("face case ids must be non-empty and unique")
        case_ids.add(case_id)
        if case.get("expected_identity") not in {"P1", "unknown"}:
            raise ValueError(f"invalid expected identity for {case_id}")
        if float(case.get("duration_s", 0)) <= 0:
            raise ValueError(f"invalid duration for {case_id}")

    passage_ids: set[str] = set()
    for passage in data.get("lpr", {}).get("passages", []):
        passage_id = str(passage.get("id", ""))
        if not passage_id or passage_id in passage_ids:
            raise ValueError("LPR passage ids must be non-empty and unique")
        passage_ids.add(passage_id)
        if float(passage.get("start_s", -1)) >= float(passage.get("end_s", -1)):
            raise ValueError(f"invalid time window for {passage_id}")
        plate = passage.get("expected_plate")
        if plate is not None and (not str(plate).isalnum() or str(plate) != str(plate).upper()):
            raise ValueError(f"expected_plate must be uppercase alphanumeric: {passage_id}")
        if passage.get("readable") and not plate:
            raise ValueError(f"readable passage requires expected_plate: {passage_id}")

    relative_sources = [
        data["face"]["enrollment"]["source"],
        *(case["source"] for case in data["face"]["cases"]),
        data["lpr"]["source"],
    ]
    for relative in relative_sources:
        if not (root / relative).is_file():
            raise FileNotFoundError(relative)
    return data


def run_ffmpeg(arguments: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *arguments, "-y"],
        check=True,
        timeout=60,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("deploy/config.yaml"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tools/fixtures/platform_baseline_ground_truth.yaml"),
    )
    parser.add_argument("--output", type=Path, default=Path(".tmp/platform-baseline"))
    args = parser.parse_args()

    started = time.monotonic()
    root = Path.cwd()
    manifest = load_manifest(args.manifest, root)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    media = output / "media"
    media.mkdir(parents=True, exist_ok=True)

    enrollment = manifest["face"]["enrollment"]
    enrollment_image = output / "enrollment-p1.jpg"
    run_ffmpeg(
        [
            "-ss",
            str(enrollment["frame_s"]),
            "-i",
            str(root / enrollment["source"]),
            "-frames:v",
            "1",
            str(enrollment_image),
        ]
    )

    base_config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    generated: dict[str, str] = {}
    for case in manifest["face"]["cases"]:
        replay = output / f"{case['id']}.mp4"
        run_ffmpeg(
            [
                "-ss",
                str(case["start_s"]),
                "-i",
                str(root / case["source"]),
                "-t",
                str(case["duration_s"]),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(replay),
            ]
        )
        config = copy.deepcopy(base_config)
        config["runtime"]["media_dir"] = str(media)
        config["runtime"]["replay"]["sources"]["face_camera"] = str(replay)
        config["notifications"]["enabled"] = False
        config["lpr"]["enabled"] = False
        config["record"]["enabled"] = False
        config_path = output / f"config-{case['id']}.yaml"
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        generated[case["id"]] = str(config_path)

    lpr_config = copy.deepcopy(base_config)
    lpr_config["runtime"]["media_dir"] = str(media)
    lpr_config["notifications"]["enabled"] = False
    lpr_config["face_recognition"]["enabled"] = False
    lpr_config["cameras"]["face_camera"]["face_recognition"]["enabled"] = False
    lpr_config["record"]["enabled"] = False
    lpr_config_path = output / "config-lpr.yaml"
    lpr_config_path.write_text(
        yaml.safe_dump(lpr_config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    generated["lpr"] = str(lpr_config_path)

    result = {
        "schema_version": 1,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": file_hash(args.manifest),
        "base_config_sha256": file_hash(args.config),
        "model_sha256": file_hash(root / base_config["runtime"]["model_path"]),
        "enrollment_image": str(enrollment_image),
        "source_sha256": {
            case["id"]: file_hash(output / f"{case['id']}.mp4")
            for case in manifest["face"]["cases"]
        }
        | {"lpr": file_hash(root / manifest["lpr"]["source"])},
        "generated_configs": generated,
        "generated_config_sha256": {
            name: file_hash(Path(path)) for name, path in generated.items()
        },
    }
    (output / "fixture.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
