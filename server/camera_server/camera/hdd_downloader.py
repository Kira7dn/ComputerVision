#!/usr/bin/env python3
"""Pull the newest existing recording from a Dahua recorder HDD.

This uses Dahua's JSON-RPC API for discovery and RPC_Loadfile for the media
payload.  It never changes RecordMode and never starts a new recording.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


def md5_upper(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


class DahuaRpcClient:
    def __init__(self, host: str, username: str, password: str, timeout: int = 30):
        self.base_url = f"http://{host}" if "://" not in host else host.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.http = requests.Session()
        self.session_id = "0"
        self.request_id = 1

    def login(self) -> None:
        challenge = self.http.post(
            f"{self.base_url}/RPC2_Login",
            json={
                "method": "global.login",
                "params": {
                    "userName": self.username,
                    "password": "",
                    "clientType": "Web3.0",
                    "loginType": "Direct",
                },
                "id": self._next_id(),
                "session": 0,
            },
            timeout=self.timeout,
        )
        challenge.raise_for_status()
        data = challenge.json()
        params = data.get("params") or {}
        realm = params.get("realm")
        random_value = params.get("random")
        if not realm or not random_value or not data.get("session"):
            raise RuntimeError(f"Dahua did not return a login challenge: {data}")

        first_hash = md5_upper(f"{self.username}:{realm}:{self.password}")
        response_hash = md5_upper(f"{self.username}:{random_value}:{first_hash}")
        result = self.http.post(
            f"{self.base_url}/RPC2_Login",
            json={
                "method": "global.login",
                "params": {
                    "userName": self.username,
                    "password": response_hash,
                    "clientType": "Web3.0",
                    "loginType": "Direct",
                    "authorityType": "Default",
                    "passwordType": "Default",
                },
                "id": self._next_id(),
                "session": data["session"],
            },
            timeout=self.timeout,
        )
        result.raise_for_status()
        logged_in = result.json()
        if not logged_in.get("result"):
            raise RuntimeError(f"Dahua login failed: {logged_in}")
        self.session_id = str(logged_in["session"])

    def rpc(self, method: str, params: Any = None, object_id: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": method,
            "params": params,
            "id": self._next_id(),
            "session": self.session_id,
        }
        if object_id is not None:
            payload["object"] = object_id
        response = self.http.post(
            f"{self.base_url}/RPC2",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("result") is False or data.get("error"):
            raise RuntimeError(f"RPC {method} failed: {data}")
        return data

    def find_recordings(self, channel: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
        created = self.rpc("mediaFileFind.factory.create")
        object_id = created["result"]
        try:
            condition = {
                "Channel": channel,
                "StartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
                "VideoStream": "Main",
            }
            self.rpc("mediaFileFind.findFile", {"condition": condition}, object_id)
            recordings: list[dict[str, Any]] = []
            while True:
                page = self.rpc("mediaFileFind.findNextFile", {"count": 100}, object_id)
                page_params = page.get("params") or {}
                infos = page_params.get("infos") or []
                recordings.extend(infos)
                if int(page_params.get("found", len(infos))) < 100:
                    return recordings
        finally:
            for method in ("mediaFileFind.close", "mediaFileFind.destroy"):
                try:
                    self.rpc(method, None, object_id)
                except Exception:
                    pass

    def download(self, recording: dict[str, Any], output_dir: Path) -> Path:
        remote_path = str(recording["FilePath"])
        filename = Path(remote_path).name
        channel = int(recording.get("Channel", 0)) + 1
        destination = output_dir / f"XVR_ch{channel}_{filename}"
        partial = destination.with_name(destination.name + ".part")
        url = (
            f"{self.base_url}/RPC_Loadfile/__download_v1__{remote_path}"
            f"/__file_name__/{quote(destination.name)}"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.http.get(
                url,
                headers=self._headers(),
                stream=True,
                timeout=(self.timeout, max(120, self.timeout)),
            ) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            output.write(chunk)
            if partial.stat().st_size == 0:
                raise RuntimeError("Dahua returned an empty media payload")
            with partial.open("rb") as media:
                if media.read(4) != b"DHAV":
                    raise RuntimeError("Downloaded payload is not a Dahua DAV file")
            os.replace(partial, destination)
            return destination
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _headers(self) -> dict[str, str]:
        return {"X-Subject-Token": self.session_id}

    def _next_id(self) -> int:
        value = self.request_id
        self.request_id += 1
        return value


def probe(path: Path) -> dict[str, Any] | None:
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return {"valid": False, "error": result.stderr.strip()}
    return {"valid": True, **json.loads(result.stdout)}


def parse_args() -> argparse.Namespace:
    default_output = Path(__file__).resolve().parents[1] / "uploads" / "videos"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.100.229")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.environ.get("DAHUA_PASSWORD"))
    parser.add_argument("--channel", type=int, default=-1, help="zero-based channel, or -1 for all")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if not args.password:
        parser.error("provide --password or DAHUA_PASSWORD")
    return args


def main() -> int:
    args = parse_args()
    client = DahuaRpcClient(args.host, args.username, args.password, args.timeout)
    client.login()
    end = datetime.now() + timedelta(minutes=1)
    start = end - timedelta(days=args.lookback_days)
    recordings = client.find_recordings(args.channel, start, end)
    if not recordings:
        raise RuntimeError("No existing HDD recordings found in the requested period")
    newest = max(recordings, key=lambda item: item.get("EndTime", ""))
    output = client.download(newest, args.output_dir.resolve())
    result = {
        "status": "downloaded",
        "output": str(output),
        "downloaded_bytes": output.stat().st_size,
        "recording": newest,
        "probe": probe(output),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
