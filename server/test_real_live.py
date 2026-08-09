#!/usr/bin/env python3
"""
Test Dahua live stream using real RPC API calls.
Based on the working DahuaRpcClient login flow from dahua_hdd_downloader.py.
"""

import os
import sys

stdout_reconfigure = getattr(sys.stdout, 'reconfigure', None)
if callable(stdout_reconfigure):
    stdout_reconfigure(encoding='utf-8', errors='replace')
stderr_reconfigure = getattr(sys.stderr, 'reconfigure', None)
if callable(stderr_reconfigure):
    stderr_reconfigure(encoding='utf-8', errors='replace')

import hashlib
import json
from datetime import datetime

import requests


def md5_upper(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


class DahuaRpcClient:
    """Same RPC client pattern as dahua_hdd_downloader.py (known working)."""

    def __init__(self, host: str, username: str, password: str, timeout: int = 30):
        self.base_url = f"http://{host}" if "://" not in host else host.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.http = requests.Session()
        self.session_id = "0"
        self.request_id = 1

    def _next_id(self) -> int:
        v = self.request_id
        self.request_id += 1
        return v

    def login(self) -> bool:
        """Login using the exact same flow as dahua_hdd_downloader.py."""
        print("[1] Sending login challenge...")
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
            print(f"   FAIL: {data}")
            return False
        print(f"   realm={realm}, random={random_value}")

        first_hash = md5_upper(f"{self.username}:{realm}:{self.password}")
        response_hash = md5_upper(f"{self.username}:{random_value}:{first_hash}")

        print("[2] Sending login with encrypted password...")
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
            print(f"   FAIL: {logged_in}")
            return False

        self.session_id = str(logged_in["session"])
        print(f"   SUCCESS! session_id={self.session_id}")
        return True

    def rpc(self, method: str, params=None, obj=None):
        """Generic RPC call (same as dahua_hdd_downloader.py)."""
        payload = {
            "method": method,
            "params": params or {},
            "id": self._next_id(),
            "session": self.session_id,
        }
        if obj is not None:
            payload["object"] = obj
        response = self.http.post(
            f"{self.base_url}/RPC2",
            json=payload,
            headers={"X-Subject-Token": self.session_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data

    def logout(self):
        try:
            self.rpc("global.logout")
            print("   Logged out.")
        except Exception as e:
            print(f"   Logout: {e}")


def test_discover_methods(client: DahuaRpcClient):
    """Try various RPC methods to find what the camera supports."""
    methods_to_try = [
        ("magicBox.getDeviceType", {}),
        ("system.getDeviceInfo", {}),
        ("configManager.getConfig", {"name": "Video"}),
        ("mediaFileFind.factory.create", {}),
    ]
    print("\n--- Discovering supported RPC methods ---")
    for method, params in methods_to_try:
        try:
            resp = client.rpc(method, params)
            has_result = resp.get("result", None)
            error = resp.get("error", None)
            if error:
                print(f"  [NO ] {method}: error={error}")
            elif has_result:
                print(f"  [YES] {method}: result={json.dumps(has_result, ensure_ascii=False)[:120]}")
            else:
                print(f"  [???] {method}: {json.dumps(resp, ensure_ascii=False)[:120]}")
        except Exception as e:
            print(f"  [ERR] {method}: {e}")


def test_start_real_play(client: DahuaRpcClient):
    """Try starting live stream with correct parameters."""
    print("\n--- Testing real-time live stream ---")

    # List of (method, params) combos - Dahuas have inconsistent naming
    combos = [
        # RealPlay via factory.create pattern (like mediaFileFind)
        ("realPlay.factory.create", {}),  # get object ID first
        # Direct start methods
        ("RealPlay", {"Channel": 0, "Commond": 0}),
        ("startRealPlay", {"channel": 0, "streamType": 0}),
        ("clientStartRealPlay", {"ipaddr": "", "port": 0, "channel": 0, "streamType": 0}),
        ("streamClient.start", {"channel": 0, "type": 0}),
    ]

    for method, params in combos:
        try:
            resp = client.rpc(method, params)
            error = resp.get("error")
            result = resp.get("result")
            if error:
                print(f"  [FAIL] {method}: error code={error.get('code')} msg={error.get('message','')}")
            elif result:
                print(f"  [OK  ] {method}: {json.dumps(result, ensure_ascii=False)[:200]}")
            else:
                print(f"  [??? ] {method}: {json.dumps(resp, ensure_ascii=False)[:200]}")
        except Exception as e:
            print(f"  [ERR ] {method}: {e}")


def test_get_rtsp_url(client: DahuaRpcClient):
    """Try to get RTSP URL from camera config."""
    print("\n--- Getting RTSP/live stream URLs ---")
    configs_to_try = [
        "Video",
        "VideoInOptions",
        "Encode",
        "RTSPServer",
        "Media",
    ]
    for name in configs_to_try:
        try:
            resp = client.rpc("configManager.getConfig", {"name": name})
            error = resp.get("error")
            if error:
                print(f"  [NO ] configManager.getConfig({name}): error code={error.get('code')}")
            else:
                result_str = json.dumps(resp.get("result", resp), ensure_ascii=False)
                # Truncate long output
                display = result_str[:300] + "..." if len(result_str) > 300 else result_str
                print(f"  [YES] configManager.getConfig({name}): {display}")
        except Exception as e:
            print(f"  [ERR] configManager.getConfig({name}): {e}")


def main():
    host = "192.168.100.229"
    username = "admin"
    password = os.environ.get("DAHUA_PASSWORD")
    if not password:
        raise RuntimeError("DAHUA_PASSWORD is required")

    print("=== Dahua Live Stream Real Test ===")
    print(f"Target: {host}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    client = DahuaRpcClient(host, username, password, timeout=30)

    # Step 1: Login
    if not client.login():
        print("\n[FAIL] Cannot login to camera - is it online?")
        return 1

    # Step 2: Discover what methods the camera supports
    test_discover_methods(client)

    # Step 3: Try to get RTSP URL from config
    test_get_rtsp_url(client)

    # Step 4: Try to start real-time stream
    test_start_real_play(client)

    # Step 5: Logout
    client.logout()

    print("\n=== Test complete ===")
    print("If any RPC method returned [OK], we found the right API for this camera.")
    print("If all returned [FAIL], we need to look up the camera's specific API documentation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
