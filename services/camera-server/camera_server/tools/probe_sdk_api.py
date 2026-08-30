"""Test Dahua official Python SDK API for live video"""

import os
import sys
from pathlib import Path

# Fix Windows console encoding
stdout_reconfigure = getattr(sys.stdout, 'reconfigure', None)
if callable(stdout_reconfigure):
    stdout_reconfigure(encoding='utf-8', errors='replace')
stderr_reconfigure = getattr(sys.stderr, 'reconfigure', None)
if callable(stderr_reconfigure):
    stderr_reconfigure(encoding='utf-8', errors='replace')

# Add dahua_sdk to path before importing
SDK_DIR = Path(__file__).resolve().parents[2] / 'vendor' / 'dahua_sdk'
sys.path.insert(0, str(SDK_DIR))

from ctypes import sizeof

from NetSDK.NetSDK import NetClient
from NetSDK.SDK_Callback import fDisConnect, fHaveReConnect
from NetSDK.SDK_Enum import EM_LOGIN_SPAC_CAP_TYPE, SDK_RealPlayType
from NetSDK.SDK_Struct import (
    C_LLONG,
    LOG_SET_PRINT_INFO,
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
)

# Global variables
login_id = C_LLONG()
play_id = C_LLONG()

def main():
    global login_id, play_id

    # 1) Initialize SDK
    sdk = NetClient()
    sdk.InitEx(fDisConnect(disconnect_callback))
    sdk.SetAutoReconnect(fHaveReConnect(reconnect_callback))

    # Optional log file
    log_info = LOG_SET_PRINT_INFO()
    log_info.dwSize = sizeof(LOG_SET_PRINT_INFO)
    log_info.bSetFilePath = 1
    log_info.szLogFilePath = b"./dahua_sdk_log.log"
    sdk.LogOpen(log_info)

    # 2) Login
    ip = "192.168.100.229"
    port = 37777
    username = "admin"
    password = os.environ.get("DAHUA_PASSWORD")
    if not password:
        raise RuntimeError("DAHUA_PASSWORD is required")
    channel = 0  # 0-based, channel 1 = 0

    stu_in = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
    stu_in.dwSize = sizeof(NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY)
    stu_in.szIP = ip.encode()
    stu_in.nPort = port
    stu_in.szUserName = username.encode()
    stu_in.szPassword = password.encode()
    stu_in.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
    stu_in.pCapParam = None

    stu_out = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
    stu_out.dwSize = sizeof(NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)

    login_id, device_info, error_msg = sdk.LoginWithHighLevelSecurity(stu_in, stu_out)
    if login_id != 0:
        print(f"Login OK! device_info.nChanNum={device_info.nChanNum}")
    else:
        print(f"Login FAILED: {error_msg}")
        sdk.Cleanup()
        return 1

    # 3) Start real-time live stream
    stream_type = SDK_RealPlayType.Realplay  # main stream
    play_id = sdk.RealPlayEx(login_id, channel, 0, stream_type)
    if play_id != 0:
        print(f"Live stream started successfully! play_id={play_id}")
    else:
        print(f"Live stream FAILED: {sdk.GetLastErrorMessage()}")
        sdk.Logout(login_id)
        sdk.Cleanup()
        return 1

    # 4) Keep alive for a bit
    print("Live stream is running. Press Enter to stop...")
    try:
        input()
    except KeyboardInterrupt:
        pass

    # 5) Stop and logout
    print("Stopping live stream...")
    sdk.StopRealPlayEx(play_id)
    play_id = 0
    sdk.Logout(login_id)
    login_id = 0
    print("Logged out.")

    # 6) Cleanup
    sdk.Cleanup()
    print("SDK cleanup done.")
    return 0


def disconnect_callback(l_login_id, pch_dvr_ip, n_dvr_port, dw_user):
    print("Disconnected from camera")


def reconnect_callback(l_login_id, pch_dvr_ip, n_dvr_port, dw_user):
    print("Reconnected to camera")


if __name__ == "__main__":
    sys.exit(main())
