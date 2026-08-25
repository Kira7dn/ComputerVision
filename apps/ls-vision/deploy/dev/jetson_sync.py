"""Synchronize apps/ls-vision changes to a native Jetson development runtime.

The Jetson owns the GPU/backend process. This watcher keeps the backend source
and configuration current over SSH; the local Vite process owns frontend HMR.
Secrets outside apps/ls-vision are intentionally never synchronized.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

IGNORED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "node_modules",
}
POLL_SECONDS = 0.75
DEBOUNCE_SECONDS = 0.35
LOCAL_ONLY_PREFIXES = ("deploy/dev/", "deploy/powershell/", "web/")


def snapshot(app_root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for directory, names, files in os.walk(app_root):
        names[:] = [name for name in names if name not in IGNORED_PARTS]
        base = Path(directory)
        for name in files:
            candidate = base / name
            try:
                stat = candidate.stat()
            except OSError:
                continue
            relative = candidate.relative_to(app_root).as_posix()
            if relative.startswith(LOCAL_ONLY_PREFIXES):
                continue
            result[relative] = (
                stat.st_mtime_ns,
                stat.st_size,
            )
    return result


def run_ssh(target: str, command: str, *, check: bool = True) -> None:
    subprocess.run(["ssh", target, command], check=check)


def remove_paths(target: str, remote_app: str, paths: list[str]) -> None:
    if not paths:
        return
    command = "rm -f -- " + " ".join(
        shlex.quote(posixpath.join(remote_app, path)) for path in paths
    )
    run_ssh(target, command)


def send_files(target: str, app_root: Path, remote_app: str, paths: list[str]) -> None:
    if not paths:
        return
    ssh = subprocess.Popen(
        ["ssh", target, f"tar -xzf - -C {shlex.quote(remote_app)}"],
        stdin=subprocess.PIPE,
    )
    assert ssh.stdin is not None
    try:
        with tarfile.open(fileobj=ssh.stdin, mode="w|gz") as archive:
            for relative in paths:
                source = app_root / Path(relative)
                if source.is_file():
                    archive.add(source, arcname=relative, recursive=False)
        ssh.stdin.close()
        return_code = ssh.wait()
    except BaseException:
        ssh.kill()
        ssh.wait()
        raise
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, ssh.args)


def sync(
    target: str,
    app_root: Path,
    remote_app: str,
    previous: dict[str, tuple[int, int]],
    current: dict[str, tuple[int, int]],
) -> None:
    changed = sorted(
        path
        for path in set(previous) | set(current)
        if current.get(path) != previous.get(path) and path in current
    )
    removed = sorted(path for path in set(previous) - set(current))
    remove_paths(target, remote_app, removed)
    send_files(target, app_root, remote_app, changed)
    if changed or removed:
        print(
            f"jetson sync: {len(changed)} updated, {len(removed)} removed",
            flush=True,
        )


def load_state(path: Path) -> dict[str, tuple[int, int]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(name): (int(values[0]), int(values[1]))
            for name, values in payload.items()
        }
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return None


def save_state(path: Path, state: dict[str, tuple[int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--jetson", required=True)
    parser.add_argument("--remote-app", default="/opt/ls-vision-dev/current/app")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS)
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_root = (args.root.resolve() / "apps" / "ls-vision").resolve()
    if not app_root.is_dir():
        raise SystemExit(f"Camera app directory was not found: {app_root}")

    current = snapshot(app_root)
    state_file = args.state_file or (args.root.resolve() / ".tmp" / "jetson-dev-sync-state.json")
    previous = load_state(state_file)
    run_ssh(args.jetson, f"mkdir -p {shlex.quote(args.remote_app)}")
    if previous is None:
        # The native dev service is provisioned by npm run deploy. Adopt its
        # current source as the first watcher baseline instead of rewriting
        # every file and restarting a healthy DMS worker on each npm run dev.
        previous = current
        save_state(state_file, current)
        print(f"jetson sync baseline recorded: {len(current)} files", flush=True)
    else:
        sync(args.jetson, app_root, args.remote_app, previous, current)
        save_state(state_file, current)
    if args.once:
        return 0

    print(f"watching {app_root} -> {args.jetson}:{args.remote_app}", flush=True)
    previous = current
    try:
        while True:
            time.sleep(max(0.1, args.interval))
            current = snapshot(app_root)
            if current == previous:
                continue
            time.sleep(DEBOUNCE_SECONDS)
            current = snapshot(app_root)
            try:
                sync(args.jetson, app_root, args.remote_app, previous, current)
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"jetson sync failed; will retry: {exc}", file=sys.stderr, flush=True)
                continue
            previous = current
            save_state(state_file, current)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
