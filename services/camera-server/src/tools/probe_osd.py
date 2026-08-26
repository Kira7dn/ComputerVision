"""Read Dahua time-OSD configuration without changing the camera."""
import os
import sys
from ctypes import sizeof
from pathlib import Path

stdout_reconfigure = getattr(sys.stdout, 'reconfigure', None)
if callable(stdout_reconfigure):
    stdout_reconfigure(encoding='utf-8', errors='replace')

SDK_DIR = Path(__file__).resolve().parents[2] / 'vendor' / 'dahua_sdk'
sys.path.insert(0, str(SDK_DIR))
from NetSDK.NetSDK import NetClient
from NetSDK.SDK_Callback import fDisConnect, fHaveReConnect
from NetSDK.SDK_Enum import EM_A_NET_EM_OSD_BLEND_TYPE, NET_EM_CFG_OPERATE_TYPE
from NetSDK.SDK_Struct import (
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OSD_TIME_TITLE,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
)

sdk = NetClient()
sdk.InitEx(fDisConnect(lambda *_: None))
sdk.SetAutoReconnect(fHaveReConnect(lambda *_: None))
login = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
login.dwSize = sizeof(login)
login.szIP = os.environ.get('DAHUA_HOST', '192.168.100.229').encode()
login.nPort = int(os.environ.get('DAHUA_PORT', '37777'))
login.szUserName = os.environ.get('DAHUA_USERNAME', 'admin').encode()
login.szPassword = os.environ['DAHUA_PASSWORD'].encode()
out = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
out.dwSize = sizeof(out)
login_id, _device, error = sdk.LoginWithHighLevelSecurity(login, out)
if not login_id:
    raise SystemExit(f'login failed: {error}')
try:
    for channel in (0, 1):
        cfg = NET_OSD_TIME_TITLE()
        cfg.dwSize = sizeof(cfg)
        cfg.emOsdBlendType = EM_A_NET_EM_OSD_BLEND_TYPE.NET_EM_OSD_BLEND_TYPE_EXTRA1
        ok = sdk.GetConfig(login_id, int(NET_EM_CFG_OPERATE_TYPE.CFG_TIMETITLE), channel, cfg, sizeof(cfg), 5000)
        changed = False
        if channel == 1 and ok and (
            os.environ.get('DAHUA_ENABLE_TIME_OSD', 'false').lower() == 'true'
            or os.environ.get('DAHUA_DISABLE_TIME_OSD', 'false').lower() == 'true'
        ):
            cfg.bEncodeBlend = os.environ.get('DAHUA_ENABLE_TIME_OSD', 'false').lower() == 'true'
            changed = bool(sdk.SetConfig(login_id, int(NET_EM_CFG_OPERATE_TYPE.CFG_TIMETITLE), channel, cfg, sizeof(cfg), 5000, 0))
        print({'channel': channel + 1, 'ok': bool(ok), 'enabled': bool(cfg.bEncodeBlend),
               'x': int(cfg.stuRect.nLeft), 'y': int(cfg.stuRect.nTop),
               'changed': changed, 'error': None if ok else sdk.GetLastErrorMessage()}, flush=True)
finally:
    sdk.Logout(login_id)
    sdk.Cleanup()
