#!/usr/bin/env python3
"""ESP32 Dashboard/WS control-plane simulator; never receives media payload."""

import argparse
import asyncio
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from aiohttp import WSMsgType, web
from requests.auth import HTTPDigestAuth

from camera_server.config import load_settings

HOST = '0.0.0.0'
PORT = 8081
MAX_CHANNELS = 8
SETTINGS = load_settings(require_dahua=False)
MEDIA_URL = SETTINGS.media_url
XVR_HOST = SETTINGS.dahua_host
XVR_BASE = f'http://{XVR_HOST}'
XVR_AUTH = None
TZ = timezone(timedelta(hours=7))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


class ControlPlane:
    def __init__(self):
        self.lock = threading.Lock()
        self.live_sessions = {}
        self.active_backups = {}
        self.logs = []

    def start_live(self, channel, subtype=0):
        self._validate_channel(channel)
        with self.lock:
            current = self.live_sessions.get(channel)
            if current:
                return current
        response = requests.post(
            f'{MEDIA_URL}/api/v1/live/start',
            json={'channel': channel, 'subtype': subtype},
            timeout=15,
        )
        response.raise_for_status()
        state = response.json()['live']
        with self.lock:
            self.live_sessions[channel] = state
        self._log('success', f'MediaServer NetSDK live ch{channel} started')
        return state

    def stop_live(self, channel):
        self._validate_channel(channel)
        with self.lock:
            session = self.live_sessions.pop(channel, None)
        if not session:
            return False
        response = requests.post(
            f'{MEDIA_URL}/api/v1/live/stop', json={'channel': channel}, timeout=15
        )
        response.raise_for_status()
        self._log('success', f'MediaServer NetSDK live ch{channel} stopped')
        return True

    def backup(self, channel, duration, source='dashboard-ws'):
        self._validate_channel(channel)
        if not 3 <= duration <= 300:
            raise ValueError('duration must be between 3 and 300 seconds')
        with self.lock:
            if channel in self.active_backups:
                raise RuntimeError(f'channel {channel} is already recording')
            item = {'id': uuid.uuid4().hex[:12], 'channel': channel, 'duration': duration, 'source': source}
            self.active_backups[channel] = item
        try:
            self._set_config(f'RecordMode[{channel - 1}].Mode', '1')
        except Exception:
            with self.lock:
                self.active_backups.pop(channel, None)
            raise
        threading.Thread(target=self._finish_backup, args=(channel, duration), daemon=True).start()
        self._log('success', f'Backup trigger {item["id"]}: ch{channel} recording {duration}s')
        return item

    def snapshot(self):
        with self.lock:
            return {
                'live': [self._live_state(channel, session) for channel, session in self.live_sessions.items()],
                'backups': list(self.active_backups.values()),
                'logs': list(self.logs),
            }

    def shutdown(self):
        with self.lock:
            sessions = list(self.live_sessions.values())
            self.live_sessions.clear()
        for session in sessions:
            try:
                requests.post(
                    f'{MEDIA_URL}/api/v1/live/stop',
                    json={'channel': session['channel']},
                    timeout=5,
                )
            except requests.RequestException:
                pass

    def _finish_backup(self, channel, duration):
        try:
            time.sleep(duration)
            index = channel - 1
            self._set_config(f'RecordMode[{index}].Mode', '2')
            time.sleep(1)
            key = f'RecordStoragePoint[{index}].TimingRecord.AutoSync'
            self._set_config(key, 'false')
            self._set_config(key, 'true')
            self._log('success', f'ch{channel} segment closed; XVR queued FTP upload')
        except Exception as exc:
            self._log('error', f'ch{channel} backup failed: {exc}')
        finally:
            with self.lock:
                self.active_backups.pop(channel, None)

    @staticmethod
    def _set_config(key, value):
        response = requests.get(
            f'{XVR_BASE}/cgi-bin/configManager.cgi',
            params={'action': 'setConfig', key: value},
            auth=XVR_AUTH,
            timeout=15,
        )
        response.raise_for_status()
        if 'OK' not in response.text.upper():
            raise RuntimeError(f'Dahua rejected {key}: {response.text.strip()}')
        # Some firmware returns OK for unsupported or immutable keys but silently
        # keeps the old value. Never report a successful vehicle-side action
        # until the recorder confirms the requested value through readback.
        config_name = key.split('[', 1)[0].split('.', 1)[0]
        readback = requests.get(
            f'{XVR_BASE}/cgi-bin/configManager.cgi',
            params={'action': 'getConfig', 'name': config_name},
            auth=XVR_AUTH,
            timeout=15,
        )
        readback.raise_for_status()
        expected_prefix = f'table.{key}='
        actual = next(
            (line.split('=', 1)[1].strip() for line in readback.text.splitlines()
             if line.startswith(expected_prefix)),
            None,
        )
        if actual is None or actual.casefold() != str(value).casefold():
            raise RuntimeError(
                f'Dahua ignored {key}: requested={value}, readback={actual}'
            )

    def _log(self, level, message):
        entry = {'time': datetime.now(TZ).strftime('%H:%M:%S'), 'level': level, 'message': message}
        with self.lock:
            self.logs = (self.logs + [entry])[-200:]
        print(f'[{entry["time"]}] [{level.upper()}] {message}', flush=True)

    @staticmethod
    def _live_state(channel, session):
        return {'channel': channel, 'control': 'media-server', **session}

    @staticmethod
    def _validate_channel(channel):
        if not 1 <= channel <= MAX_CHANNELS:
            raise ValueError(f'channel must be between 1 and {MAX_CHANNELS}')


control_plane = ControlPlane()

DASHBOARD_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Letron ESP32 Mock</title><style>
*{box-sizing:border-box}body{margin:0;background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif}header{padding:16px 24px;background:#1e293b;border-bottom:1px solid #334155;display:flex;justify-content:space-between}h1{font-size:18px;margin:0}.status{color:#22c55e;font-size:13px}.wrap{padding:20px 24px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}@media(max-width:1200px){.grid{grid-template-columns:repeat(2,1fr)}}.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px}.card h3{margin:0 0 10px}.card iframe{width:100%;height:180px;background:#020617;border:0;border-radius:6px}.meta{font-size:12px;color:#94a3b8;min-height:34px;padding:8px 0}.actions{display:flex;gap:8px}.actions button{flex:1;padding:10px;border:0;border-radius:7px;color:white;font-weight:700;cursor:pointer}.live{background:#059669}.backup{background:#2563eb}.stop{background:#dc2626!important}.title{margin:24px 0 10px;color:#94a3b8}.logs{background:#020617;padding:12px;border-radius:8px;max-height:260px;overflow:auto;font:12px monospace}.success{color:#22c55e}.error{color:#ef4444}
</style></head><body><header><h1>Letron ADAS Dashboard</h1><span class="status" id="status">WS connecting · ADAS video grid</span></header><main class="wrap"><div class="grid" id="channels"></div><div class="title">Runtime logs</div><div class="logs" id="logs"></div></main><script>
const MEDIA='http://192.168.100.108:8080';let ws,live=new Set();const channels=document.getElementById('channels');channels.innerHTML=Array.from({length:8},(_,i)=>`<div class="card"><h3>Camera ${i+1}</h3><iframe id="video-${i+1}" allow="autoplay;fullscreen"></iframe><div class="meta" id="tag-${i+1}">ADAS chưa khởi động</div><div class="actions"><button class="live" id="live-${i+1}" onclick="toggleLive(${i+1})">Start ADAS</button><button class="backup" onclick="send({action:'backup',channel:${i+1},duration:15})">Backup 15s</button></div></div>`).join('');
function connect(){ws=new WebSocket(`ws://${location.host}/ws`);ws.onopen=()=>{document.getElementById('status').textContent='WS connected · ADAS dashboard';send({action:'state'});for(let ch=1;ch<=8;ch++)send({action:'live_start',channel:ch,subtype:0})};ws.onclose=()=>{document.getElementById('status').textContent='WS disconnected';setTimeout(connect,1500)};ws.onmessage=e=>render(JSON.parse(e.data))}function send(x){if(ws.readyState===1)ws.send(JSON.stringify(x))}
function toggleLive(ch){send({action:live.has(ch)?'live_stop':'live_start',channel:ch,subtype:0})}
async function refreshMedia(){try{const s=await(await fetch(`${MEDIA}/api/state`,{cache:'no-store'})).json();const seen=new Set();for(const v of s.live_streams){seen.add(v.channel);const tag=document.getElementById(`tag-${v.channel}`),frame=document.getElementById(`video-${v.channel}`);if(v.adas?.status==='healthy'){const url=v.adas.webrtc_url;if(frame.src!==url)frame.src=url;tag.textContent=`ADAS processed · publish P95 ${v.adas.decode_to_publish?.p95_ms??'?'} ms · detect P95 ${v.adas.decode_to_detection?.p95_ms??'?'} ms`}else{if(frame.src)frame.removeAttribute('src');tag.textContent=v.adas?.last_error||'Đang chờ ADAS'}}for(let ch=1;ch<=8;ch++){if(!seen.has(ch)){const frame=document.getElementById(`video-${ch}`);if(frame.src)frame.removeAttribute('src');document.getElementById(`tag-${ch}`).textContent='Đang chờ ADAS'}}}catch(e){document.getElementById('status').textContent='Media/ADAS unavailable'}}
function render(x){if(x.type==='state'){live=new Set(x.live.map(v=>v.channel));for(let ch=1;ch<=8;ch++){const b=document.getElementById('live-'+ch);b.textContent=live.has(ch)?'Stop ADAS':'Start ADAS';b.classList.toggle('stop',live.has(ch))}document.getElementById('logs').innerHTML=x.logs.slice().reverse().map(v=>`<div class="${v.level}">[${v.time}] ${v.message}</div>`).join('');refreshMedia()}}connect();setInterval(refreshMedia,2000);
</script></body></html>'''


async def index(_request):
    return web.Response(text=DASHBOARD_HTML, content_type='text/html')


async def health(_request):
    return web.json_response({'status': 'ok', 'service': 'adas-dashboard', 'media_payload_processing': True})


async def favicon(_request):
    return web.Response(status=204)


async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    async def send_state():
        try:
            await ws.send_json({'type': 'state', **control_plane.snapshot()})
            return True
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError) as exc:
            if not isinstance(exc, asyncio.CancelledError):
                control_plane._log('warning', f'dashboard websocket closed: {exc}')
            return False

    if not await send_state():
        return ws
    async for message in ws:
        if message.type != WSMsgType.TEXT:
            continue
        payload = {}
        try:
            payload = json.loads(message.data)
            action = payload.get('action')
            channel = int(payload.get('channel', 0))
            if action == 'state':
                await send_state(); continue
            if action == 'live_start':
                await asyncio.to_thread(control_plane.start_live, channel, int(payload.get('subtype', 0)))
            elif action == 'live_stop':
                await asyncio.to_thread(control_plane.stop_live, channel)
            elif action == 'backup':
                await asyncio.to_thread(control_plane.backup, channel, int(payload.get('duration', 15)))
            else:
                raise ValueError(f'unsupported action: {action}')
            await ws.send_json({'type': 'action_result', 'ok': True, 'action': action, 'channel': channel})
            if not await send_state():
                break
        except Exception as exc:
            await ws.send_json({'type': 'action_result', 'ok': False, 'action': payload.get('action'), 'channel': payload.get('channel'), 'message': str(exc)})
    return ws


def main():
    global XVR_AUTH
    XVR_AUTH = HTTPDigestAuth(SETTINGS.dahua_username, SETTINGS.dahua_password)
    parser = argparse.ArgumentParser(description='ESP32 Dashboard/WS simulator')
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health)
    app.router.add_get('/favicon.ico', favicon)
    app.router.add_get('/ws', websocket_handler)
    app.on_shutdown.append(lambda _app: asyncio.to_thread(control_plane.shutdown))
    web.run_app(app, host=args.host, port=args.port, print=lambda text: print(text, flush=True))


if __name__ == '__main__':
    main()
