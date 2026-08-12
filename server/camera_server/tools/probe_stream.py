#!/usr/bin/env python3
"""Bounded real-device probe for NetSDK H264 versus TS callback contracts."""

import argparse
import collections
import json
import os
import subprocess
import threading
import time
from ctypes import POINTER, c_byte, c_ubyte, cast, sizeof
from pathlib import Path

from camera_server.media.netsdk_source import (
    CB_FUNCTYPE,
    C_DWORD,
    C_LDWORD,
    C_LLONG,
    EM_LOGIN_SPAC_CAP_TYPE,
    EM_REAL_DATA_TYPE,
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_IN_REALPLAY_BY_DATA_TYPE,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OUT_REALPLAY_BY_DATA_TYPE,
    NetClient,
    SDK_RealPlayType,
    fDisConnect,
    fHaveReConnect,
)


MAX_CAPTURE_BYTES = 16 * 1024 * 1024
fRealDataEx = CB_FUNCTYPE(
    None, C_LLONG, C_DWORD, POINTER(c_byte), C_DWORD, C_LLONG, C_LDWORD
)


def login(sdk, args):
    input_ = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
    input_.dwSize = sizeof(input_)
    input_.szIP = args.host.encode()
    input_.nPort = args.port
    input_.szUserName = args.username.encode()
    input_.szPassword = args.password.encode()
    input_.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
    output = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
    output.dwSize = sizeof(output)
    login_id, _device, error = sdk.LoginWithHighLevelSecurity(input_, output)
    if not login_id:
        raise RuntimeError(error)
    return login_id


def probe_file(path, mode):
    format_name = 'h264' if mode == 'h264' else 'mpegts'
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-f', format_name,
        '-show_entries', 'stream=codec_name,width,height,r_frame_rate',
        '-of', 'json', str(path),
    ], capture_output=True, text=True, timeout=15)
    return {
        'ok': result.returncode == 0,
        'probe': json.loads(result.stdout or '{}'),
        'stderr': result.stderr.strip(),
    }


def run_round(sdk, args, mode, round_index, output_dir):
    login_id = login(sdk, args)
    path = output_dir / f'{mode}-round-{round_index:02d}.bin'
    counts = collections.Counter()
    callback_times = []
    lock = threading.Lock()
    captured = 0

    def on_data(_handle, data_type, buffer, size, _param, _user):
        nonlocal captured
        now = time.monotonic()
        with lock:
            counts[int(data_type)] += 1
            callback_times.append(now)
            remaining = MAX_CAPTURE_BYTES - captured
            if remaining <= 0:
                return
            length = min(int(size), remaining)
            data = bytes(cast(buffer, POINTER(c_ubyte * length)).contents)
            with path.open('ab') as target:
                target.write(data)
            captured += length

    callback = fRealDataEx(on_data)
    input_ = NET_IN_REALPLAY_BY_DATA_TYPE()
    input_.dwSize = sizeof(input_)
    input_.nChannelID = args.channel - 1
    input_.hWnd = 0
    input_.rType = SDK_RealPlayType.Realplay_1 if args.subtype == 1 else SDK_RealPlayType.Realplay
    input_.cbRealDataEx = callback
    input_.emDataType = EM_REAL_DATA_TYPE.H264 if mode == 'h264' else EM_REAL_DATA_TYPE.TS
    output = NET_OUT_REALPLAY_BY_DATA_TYPE()
    output.dwSize = sizeof(output)
    play_id = sdk.RealPlayByDataType(login_id, input_, output, 10000)
    if not play_id:
        sdk.Logout(login_id)
        raise RuntimeError(sdk.GetLastErrorMessage())
    try:
        time.sleep(args.duration)
    finally:
        sdk.StopRealPlayEx(play_id)
        sdk.Logout(login_id)
    intervals = [
        (right - left) * 1000 for left, right in zip(callback_times, callback_times[1:])
    ]
    return {
        'round': round_index,
        'mode': mode,
        'bytes': captured,
        'callback_types': dict(counts),
        'callback_count': len(callback_times),
        'max_interarrival_ms': round(max(intervals), 2) if intervals else None,
        **probe_file(path, mode),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='192.168.100.229')
    parser.add_argument('--port', type=int, default=37777)
    parser.add_argument('--username', default='admin')
    parser.add_argument('--password', default=os.environ.get('DAHUA_PASSWORD'))
    parser.add_argument('--channel', type=int, default=2)
    parser.add_argument('--subtype', type=int, default=1)
    parser.add_argument('--duration', type=float, default=3)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--output', default='runtime/artifacts/netsdk-probe')
    args = parser.parse_args()
    if not args.password:
        parser.error('set DAHUA_PASSWORD or pass --password')

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sdk = NetClient()
    disconnect = fDisConnect(lambda *_: None)
    reconnect = fHaveReConnect(lambda *_: None)
    sdk.InitEx(disconnect)
    sdk.SetAutoReconnect(reconnect)
    results = []
    try:
        for mode in ('h264', 'ts'):
            for index in range(1, args.rounds + 1):
                results.append(run_round(sdk, args, mode, index, output_dir))
    finally:
        sdk.Cleanup()
    summary = {
        'channel': args.channel,
        'subtype': args.subtype,
        'rounds': results,
        'h264_pass': all(item['ok'] and item['bytes'] for item in results if item['mode'] == 'h264'),
        'ts_pass': all(item['ok'] and item['bytes'] for item in results if item['mode'] == 'ts'),
    }
    (output_dir / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
