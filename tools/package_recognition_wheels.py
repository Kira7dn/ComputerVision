"""Build the two recognition distributions from the checked-in source owner.

The staging tree is disposable build input; no second implementation is kept in
the repository.  The manifest records the source commit and worktree hash so a
wheel cannot be mistaken for an untraceable copied runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECOGNITION = ROOT / "frigate" / "frigate" / "recognition"
CORE_FILES = ("__init__.py", "contracts.py", "ports.py", "face.py", "lpr.py", "core.py", "executor.py")
CLIENT_FILES = (
    "service/__init__.py", "service/config_fingerprint.py", "service/evidence.py",
    "service/grpc_client.py", "service/grpc_server.py", "service/health.py",
    "service/health_pb2.py", "service/health_pb2_grpc.py", "service/wire.py",
    "service/v1/__init__.py", "service/v1/recognition.proto",
    "service/v1/recognition_pb2.py", "service/v1/recognition_pb2_grpc.py",
)


def digest_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts:
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(("git", *args), cwd=ROOT, text=True).strip()


def make_project(root: Path, name: str, files: tuple[str, ...], dependency: list[str]) -> None:
    source = root / "src" / "frigate" / "recognition"
    source.mkdir(parents=True)
    for relative in files:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RECOGNITION / relative, target)
    (root / "pyproject.toml").write_text(
        """[build-system]\nrequires = [\"setuptools>=68\", \"wheel\"]\nbuild-backend = \"setuptools.build_meta\"\n\n[project]\nname = \"%s\"\nversion = \"0.1.0\"\nrequires-python = \">=3.11,<3.12\"\ndependencies = %s\n\n[tool.setuptools]\npackage-dir = {\"\" = \"src\"}\n\n[tool.setuptools.packages.find]\nwhere = [\"src\"]\ninclude = [\"frigate.recognition*\"]\n\n[tool.setuptools.package-data]\n\"frigate.recognition\" = [\"service/v1/*.proto\"]\n""" % (name, json.dumps(dependency)),
        encoding="utf-8",
    )


def build(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recognition-wheel-") as temporary:
        staging = Path(temporary)
        make_project(staging / "core", "frigate-recognition-core", CORE_FILES, ["rapidfuzz>=3.12,<4"])
        make_project(
            staging / "client", "frigate-recognition-client", CLIENT_FILES,
            ["frigate-recognition-core==0.1.0", "grpcio==1.82.0", "protobuf>=5,<7", "numpy>=1.26,<2"],
        )
        for project in (staging / "core", staging / "client"):
            build_env = os.environ.copy()
            # ZIP timestamps start at 1980; this fixed epoch also makes two
            # builds from the same source byte-for-byte reproducible.
            build_env["SOURCE_DATE_EPOCH"] = "315532800"
            subprocess.run(("python", "-m", "pip", "wheel", str(project), "--no-deps", "--ignore-requires-python", "--wheel-dir", str(output)), check=True, env=build_env)
    wheels = sorted(output.glob("*.whl"))
    manifest = {
        "schema_version": 1,
        "source_commit": git("rev-parse", "HEAD"),
        "source_worktree_hash": digest_tree(RECOGNITION),
        "wheels": [{"name": item.name, "sha256": hashlib.sha256(item.read_bytes()).hexdigest(), "bytes": item.stat().st_size} for item in wheels],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "recognition-wheels")
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
