"""Normalize the human JSON annotation form without requiring bboxes."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def normalize_face(value: Any) -> tuple[str | None, bool]:
    text = str(value or "").strip().lower()
    if "không có người" in text or "no person" in text:
        return None, False
    if "chưa tạo danh tính" in text or "unknown" in text or "người mới" in text:
        return "unknown", True
    if text:
        return str(value).strip(), True
    return None, False


def normalize_lpr(readable: Any, plate: Any) -> tuple[bool, str | None, str | None]:
    plate_text = str(plate or "").strip().upper()
    if plate_text and re.fullmatch(r"[A-Z0-9]+", plate_text):
        return True, plate_text, None
    combined = f"{readable or ''} {plate or ''}".lower()
    if "không có xe" in combined or "chưa vào vùng" in combined or "chưa rõ" in combined:
        return False, None, combined.strip()
    return False, None, "missing readable plate label"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path(".tmp/platform-passage/annotation/annotation_form.json"))
    parser.add_argument("--output", type=Path, default=Path(".tmp/platform-passage/annotation/normalized_ground_truth.json"))
    args = parser.parse_args()
    value = json.loads(args.input.read_text(encoding="utf-8"))
    result: dict[str, Any] = {"schema_version": 1, "bbox_required": False, "face": [], "lpr": [], "gates": {}}
    for row in value.get("face", []):
        identity, valid = normalize_face(row.get("identity"))
        result["face"].append({"id": row["id"], "expected_identity": identity, "valid_passage": valid, "bbox": None, "note": row.get("bbox")})
    for row in value.get("lpr", []):
        readable, plate, reason = normalize_lpr(row.get("readable"), row.get("expected_plate"))
        result["lpr"].append({"id": row["id"], "readable": readable, "expected_plate": plate, "valid_passage": bool(plate or "không có xe" not in str(reason or "").lower()), "bbox": None, "roi": None, "note": reason})
    known = [r for r in result["face"] if r["valid_passage"] and r["expected_identity"] not in (None, "unknown")]
    unknown = [r for r in result["face"] if r["valid_passage"] and r["expected_identity"] == "unknown"]
    readable = [r for r in result["lpr"] if r["valid_passage"] and r["readable"]]
    result["gates"] = {"face_known_at_least_2": len(known) >= 2, "face_unknown_at_least_2": len(unknown) >= 2, "lpr_readable_at_least_3": len(readable) >= 3, "bbox_required": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "gates": result["gates"]}, ensure_ascii=False))
    return 0 if all(result["gates"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
