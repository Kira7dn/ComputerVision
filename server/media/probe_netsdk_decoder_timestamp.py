"""Bounded probe for Dahua PLAYSDK decoded-frame timestamps.

This intentionally uses the SDK's documented RealPlayEx -> PLAYSDK decoder path,
separate from the production TS/NVDEC session. It reports PLAY_FRAME_INFO.nStamp.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from ctypes import POINTER, c_ubyte, cast, sizeof
from pathlib import Path

SDK_DIR = Path(__file__).resolve().parent.parent / 'dahua_sdk'
sys.path.insert(0, str(SDK_DIR))

from NetSDK.NetSDK import NetClient
from NetSDK.SDK_Callback import fDecCBFun, fDisConnect, fHaveReConnect, fRealDataCallBackEx2
from NetSDK.SDK_Enum import EM_LOGIN_SPAC_CAP_TYPE, EM_REALDATA_FLAG, SDK_RealPlayType
from NetSDK.SDK_Enum import EM_REAL_DATA_TYPE
from NetSDK.SDK_Struct import (
    C_LDWORD,
    C_LLONG,
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
    PLAY_FRAME_INFO,
    NET_IN_REALPLAY_BY_DATA_TYPE,
    NET_OUT_REALPLAY_BY_DATA_TYPE,
)


class Probe:
    def __init__(self):
        self.sdk = NetClient()
        self.login_id = C_LLONG()
        self.play_id = C_LLONG()
        self.port = None
        self.window = None
        self.stop_event = threading.Event()
        self.first_callback = None
        self.first_frame = None
        self.frames = 0
        self.decode_callbacks = 0
        self.decode_types = {}
        self.input_calls = 0
        self.input_failures = 0
        self.stamps = []
        self.frame_callback = fDecCBFun(self.on_decoded)
        self.data_callback = fRealDataCallBackEx2(self.on_data)
        self.disconnect_callback = fDisConnect(lambda *_args: None)
        self.reconnect_callback = fHaveReConnect(lambda *_args: None)
        self.sdk.InitEx(self.disconnect_callback)
        self.sdk.SetAutoReconnect(self.reconnect_callback)

    def login(self):
        request = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
        request.dwSize = sizeof(request)
        request.szIP = os.environ.get('DAHUA_HOST', '192.168.100.229').encode()
        request.nPort = int(os.environ.get('DAHUA_PORT', '37777'))
        request.szUserName = os.environ.get('DAHUA_USERNAME', 'admin').encode()
        request.szPassword = os.environ.get('DAHUA_PASSWORD', 'letron123').encode()
        request.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
        request.pCapParam = None
        response = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
        response.dwSize = sizeof(response)
        self.login_id, _device, error = self.sdk.LoginWithHighLevelSecurity(request, response)
        if not self.login_id:
            raise RuntimeError(f'login failed: {error}')

    def start(self, channel, subtype):
        ok, self.port = self.sdk.GetFreePort()
        if not ok or not self.sdk.OpenStream(self.port):
            raise RuntimeError('PLAYSDK port/open stream failed')
        hwnd = 0
        if os.environ.get('DAHUA_PROBE_HIDDEN_WINDOW', 'true').lower() == 'true':
            try:
                import tkinter
                self.window = tkinter.Tk()
                self.window.title('Dahua timestamp probe')
                self.window.geometry('16x16+0+0')
                self.window.withdraw()
                self.window.update_idletasks()
                hwnd = self.window.winfo_id()
            except Exception as exc:
                raise RuntimeError(f'cannot create probe window: {exc}') from exc
        if not self.sdk.Play(self.port, hwnd):
            raise RuntimeError('PLAYSDK Play failed')
        play_in = NET_IN_REALPLAY_BY_DATA_TYPE()
        play_in.dwSize = sizeof(play_in)
        play_in.nChannelID = channel - 1
        play_in.hWnd = 0
        play_in.rType = SDK_RealPlayType.Realplay if subtype == 0 else SDK_RealPlayType.Realplay_1
        play_in.cbRealDataEx = self.data_callback
        play_in.emDataType = EM_REAL_DATA_TYPE.H264
        play_out = NET_OUT_REALPLAY_BY_DATA_TYPE()
        play_out.dwSize = sizeof(play_out)
        self.play_id = self.sdk.RealPlayByDataType(self.login_id, play_in, play_out, 10000)
        if not self.play_id:
            raise RuntimeError(f'RealPlayEx failed: {self.sdk.GetLastErrorMessage()}')
        if not self.sdk.SetDecCallBack(self.port, self.frame_callback):
            raise RuntimeError('PLAYSDK SetDecCallBack failed')

    def on_data(self, handle, data_type, buffer, size, _param, _user):
        if handle != self.play_id or int(data_type) < 1000 or not size or self.stop_event.is_set():
            return
        now = time.monotonic()
        if self.first_callback is None:
            self.first_callback = now
        self.input_calls += 1
        if not self.sdk.InputData(self.port, buffer, size):
            self.input_failures += 1

    def on_decoded(self, port, _buffer, _size, info_ptr, _user, _reserved):
        if port != self.port or not info_ptr:
            return
        info = info_ptr.contents
        self.decode_callbacks += 1
        key = str(int(info.nType))
        self.decode_types[key] = self.decode_types.get(key, 0) + 1
        if info.nType != 3:
            return
        now = time.monotonic()
        if self.first_frame is None:
            self.first_frame = now
        self.frames += 1
        self.stamps.append(int(info.nStamp))
        if len(self.stamps) > 256:
            self.stamps.pop(0)

    def stop(self):
        self.stop_event.set()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None
        if self.play_id:
            self.sdk.StopRealPlayEx(self.play_id)
            self.play_id = C_LLONG()
        if self.port is not None:
            self.sdk.SetDecCallBack(self.port, None)
            self.sdk.Stop(self.port)
            self.sdk.CloseStream(self.port)
            self.sdk.ReleasePort(self.port)
            self.port = None
        if self.login_id:
            self.sdk.Logout(self.login_id)
            self.login_id = C_LLONG()
        self.sdk.Cleanup()

    def report(self):
        monotonic = [b - a for a, b in zip(self.stamps, self.stamps[1:]) if b >= a]
        return {
            'frames': self.frames,
            'decode_callbacks': self.decode_callbacks,
            'decode_types': self.decode_types,
            'input_calls': self.input_calls,
            'input_failures': self.input_failures,
            'time_to_first_callback_ms': None if self.first_callback is None else round((self.first_callback - self.started) * 1000, 2),
            'time_to_first_decoded_frame_ms': None if self.first_frame is None else round((self.first_frame - self.started) * 1000, 2),
            'stamp_count': len(self.stamps),
            'stamp_first_ms': self.stamps[0] if self.stamps else None,
            'stamp_last_ms': self.stamps[-1] if self.stamps else None,
            'stamp_monotonic': bool(self.stamps) and len(monotonic) == len(self.stamps) - 1,
            'stamp_step_p50_ms': sorted(monotonic)[len(monotonic) // 2] if monotonic else None,
        }

    def run(self, channel, subtype, seconds):
        self.started = time.monotonic()
        try:
            self.login()
            self.start(channel, subtype)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if self.window is not None:
                    self.window.update()
                time.sleep(0.02)
            return self.report()
        finally:
            self.stop()


if __name__ == '__main__':
    probe = Probe()
    print(probe.run(int(os.environ.get('DAHUA_PROBE_CHANNEL', '2')), 1, float(os.environ.get('DAHUA_PROBE_SECONDS', '10'))), flush=True)
