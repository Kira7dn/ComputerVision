#!/usr/bin/env python3
"""Standalone Dahua MediaServer: multicast ingest, HLS delivery, FTP media UI."""

import argparse
import http.server
import json
import mimetypes
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from netsdk_hls import NetSdkHlsManager
from cloud_queue import UploadQueue

HOST = '0.0.0.0'
PORT = 8080
MAX_CHANNELS = 8
TZ = timezone(timedelta(hours=7))
SERVER_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = Path(__file__).resolve().parent / 'assets'
VIDEO_DIR = SERVER_ROOT / 'uploads' / 'videos'
LIVE_DIR = SERVER_ROOT / 'uploads' / 'live'
CLOUD_QUEUE_DB = SERVER_ROOT / 'uploads' / 'cloud_queue.sqlite3'
VIDEO_EXTENSIONS = {'.dav', '.mp4', '.mov', '.avi', '.mkv', '.ts', '.265', '.h265'}
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


class EventLog:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []

    def add(self, level, message):
        entry = {'time': datetime.now(TZ).strftime('%H:%M:%S'), 'level': level, 'message': message}
        with self.lock:
            self.entries = (self.entries + [entry])[-200:]
        print(f'[{entry["time"]}] [{level.upper()}] {message}', flush=True)

    def snapshot(self):
        with self.lock:
            return list(self.entries)


event_log = EventLog()
live_streams = NetSdkHlsManager(LIVE_DIR, event_log)
cloud_queue = UploadQueue(CLOUD_QUEUE_DB, VIDEO_DIR)


def list_videos():
    videos = []
    for path in VIDEO_DIR.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        relative = path.relative_to(VIDEO_DIR).as_posix()
        match = re.search(r'(?:XVR_)?ch(\d+)', path.name, re.IGNORECASE)
        stat = path.stat()
        videos.append({
            'relative_path': relative,
            'url': '/uploads/videos/' + quote(relative, safe='/'),
            'filename': path.name,
            'channel': int(match.group(1)) if match else None,
            'size_kb': round(stat.st_size / 1024, 1),
            'uploaded_at': datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(),
            'uploaded_at_display': datetime.fromtimestamp(stat.st_mtime, TZ).strftime('%Y-%m-%d %H:%M:%S'),
        })
    return sorted(videos, key=lambda item: item['uploaded_at'], reverse=True)


MEDIA_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Letron MediaServer</title><style>
*{box-sizing:border-box}body{margin:0;background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif}header{padding:16px 24px;background:#1e293b;border-bottom:1px solid #334155;display:flex;justify-content:space-between}h1{font-size:18px;margin:0}.status{color:#22c55e;font-size:13px}.wrap{padding:20px 24px}.notice{padding:12px;background:#1e293b;border:1px solid #334155;border-radius:8px;color:#94a3b8}.stop{padding:9px 14px;border:0;border-radius:7px;color:white;font-weight:700;cursor:pointer;background:#dc2626}.player{display:none;margin-top:16px;background:#020617;border:1px solid #334155;border-radius:10px;padding:12px}.player.active{display:block}.player video,.player iframe{width:100%;height:65vh;max-height:65vh;background:#000;border:0}.player iframe{display:none}.player-head{display:flex;justify-content:space-between;margin-bottom:8px}.title{margin:24px 0 10px;color:#94a3b8;font-size:14px}.videos{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}.video{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px}.video a{color:#38bdf8;word-break:break-all}.meta{font-size:12px;color:#94a3b8;margin-top:6px}.empty{color:#64748b;padding:20px}
</style></head><body><header><h1>Letron MediaServer</h1><span class="status">Media UI/HLS :8080 · FTP :2121</span></header><main class="wrap">
<div class="notice">ADAS mode dùng NetSDK → NVDEC → TensorRT → NVENC → WebRTC. HLS chỉ là fallback.</div><div class="player" id="player"><div class="player-head"><strong id="player-title">Live</strong><button class="stop" onclick="stopView()">Đóng player</button></div><iframe id="webrtc" allow="autoplay; fullscreen"></iframe><video id="video" controls autoplay muted playsinline></video><div class="meta" id="live-status"></div></div>
<div class="title">Dahua FTP uploads</div><div class="videos" id="videos"></div></main><script src="/assets/hls.min.js"></script><script>
let channel=null,hls=null;
async function view(ch){channel=ch;document.getElementById('player').classList.add('active');document.getElementById('player-title').textContent=`Live channel ${ch}`;document.getElementById('live-status').textContent='Đang mở Dahua NetSDK stream...';const r=await fetch('/api/live/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel:ch,subtype:ch===2?0:0})});const x=await r.json();if(!r.ok){document.getElementById('live-status').textContent=x.message||'Ingest failed';return}await attach(x.live.playlist_url)}
async function attach(url){let live=null;for(let i=0;i<60;i++){const state=await(await fetch('/api/state',{cache:'no-store'})).json();live=state.live_streams.find(x=>x.channel===channel);if(live?.adas?.status==='healthy'||(!live?.adas&&live?.status==='ready'))break;await new Promise(x=>setTimeout(x,500))}const frame=document.getElementById('webrtc');const v=document.getElementById('video');if(live?.adas?.status==='healthy'){v.style.display='none';frame.style.display='block';frame.src=live.adas.webrtc_url;const p=live.adas.decode_to_publish||{};const d=live.adas.decode_to_detection||{};const t=live.adas.dahua_timestamp?.verified?'Dahua PTS→publish '+(live.adas.dahua_to_publish?.p95_ms??'?')+' ms P95':'Dahua PTS unavailable';document.getElementById('live-status').textContent=`WebRTC ADAS · tổng server P95 ${p.p95_ms??'?'} ms · detect ${d.p95_ms??'?'} ms · ${t}`;return}frame.style.display='none';v.style.display='block';if(!live||live.status!=='ready'){document.getElementById('live-status').textContent=live?.adas?.last_error||'NetSDK chưa nhận được video hợp lệ.';return}if(hls)hls.destroy();if(window.Hls&&Hls.isSupported()){hls=new Hls({liveSyncDurationCount:2});hls.loadSource(url);hls.attachMedia(v);hls.on(Hls.Events.MANIFEST_PARSED,()=>{document.getElementById('live-status').textContent='HLS fallback';v.play().catch(()=>{})})}else{v.src=url;v.play().catch(()=>{})}}
async function stopView(){if(channel)await fetch('/api/live/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel})});if(hls){hls.destroy();hls=null}const frame=document.getElementById('webrtc');frame.removeAttribute('src');frame.style.display='none';const v=document.getElementById('video');v.removeAttribute('src');v.load();document.getElementById('player').classList.remove('active');channel=null}
async function refresh(){const s=await(await fetch('/api/state')).json();const cloud=new Map(s.cloud_uploads.map(x=>[x.relative_path,x]));document.getElementById('videos').innerHTML=s.videos.length?s.videos.map(v=>{const q=cloud.get(v.relative_path);return `<div class="video"><a href="${v.url}" target="_blank">${v.filename}</a><div class="meta">Channel ${v.channel||'?'} · ${v.size_kb} KB · ${v.uploaded_at_display} · cloud=${q?q.status:'not-queued'}</div></div>`}).join(''):'<div class="empty">Chưa có video upload</div>'}refresh();setInterval(refresh,3000);const requested=Number(new URLSearchParams(location.search).get('channel'));if(requested>=1&&requested<=8)view(requested);
</script></body></html>'''


class MediaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/media'):
            self._bytes(MEDIA_HTML.encode('utf-8'), 'text/html; charset=utf-8')
        elif path == '/favicon.ico':
            self.send_response(204); self.end_headers()
        elif path == '/health':
            self._json({'status': 'ok', 'service': 'dahua-media-server', 'dahua_control': True, 'live_transport': 'netsdk-ts', 'live_streams': live_streams.snapshot()})
        elif path == '/api/state':
            videos = list_videos()
            self._json({'videos': videos, 'total_videos': len(videos), 'live_streams': live_streams.snapshot(), 'cloud_uploads': cloud_queue.snapshot(), 'logs': event_log.snapshot()})
        elif path == '/assets/hls.min.js':
            self._file(ASSET_DIR / 'hls.min.js', 'text/javascript; charset=utf-8')
        elif path.startswith('/live/'):
            target = self._safe_path(LIVE_DIR, path[len('/live/'):])
            if target.suffix == '.m3u8' and not target.is_file():
                self._json({'status': 'not_ready', 'message': 'Trigger Live from ESP32MockServer :8081 before opening this playlist'}, 425)
            else:
                self._file(target, 'application/vnd.apple.mpegurl' if target.suffix == '.m3u8' else 'video/mp2t', True)
        elif path.startswith('/uploads/videos/'):
            target = self._safe_path(VIDEO_DIR, path[len('/uploads/videos/'):])
            self._file(target, mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/live/start', '/api/live/stop'):
            self.send_error(404); return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 16384:
                raise ValueError('invalid JSON body')
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            channel = int(payload.get('channel'))
            if path.endswith('/start'):
                self._json({'status': 'accepted', 'live': live_streams.start(channel, int(payload.get('subtype', 0)))}, 202)
            else:
                self._json({'status': 'ok', 'stopped': live_streams.stop(channel)})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json({'status': 'error', 'message': str(exc)}, 400)
        except Exception as exc:
            event_log.add('error', f'Media API failed: {exc}')
            self._json({'status': 'error', 'message': str(exc)}, 502)

    def _safe_path(self, root, relative):
        target = (root / relative).resolve()
        if root.resolve() != target and root.resolve() not in target.parents:
            raise PermissionError('path outside media root')
        return target

    def _file(self, target, content_type, no_cache=False):
        if not target.is_file():
            self.send_error(404); return
        self._bytes(target.read_bytes(), content_type, no_cache)

    def _bytes(self, data, content_type, no_cache=False):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        if no_cache:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Content-Length', len(data))
        self.end_headers(); self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description='Standalone Dahua MediaServer')
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--public-host', default='192.168.100.108')
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer((args.host, args.port), MediaHandler)
    print(f'MediaServer: http://{args.public_host}:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        live_streams.shutdown()


if __name__ == '__main__':
    main()
