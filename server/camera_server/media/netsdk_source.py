"""Dahua NetSDK real-play ingest -> MPEG-TS callback -> FFmpeg HLS."""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from ctypes import POINTER, c_byte, c_ubyte, cast, sizeof
from pathlib import Path

# ADAS loaded lazily — only when ADAS_ENABLED=true and a matching channel is opened


SDK_DIR = Path(__file__).resolve().parents[3] / 'sdk'
sys.path.insert(0, str(SDK_DIR))

from NetSDK.NetSDK import NetClient
from NetSDK.SDK_Callback import CB_FUNCTYPE, fDataCallBackEx, fDisConnect, fHaveReConnect
from NetSDK.SDK_Enum import (
    EM_LOGIN_SPAC_CAP_TYPE,
    EM_REAL_DATA_TYPE,
    SDK_RealPlayType,
)
from NetSDK.SDK_Struct import (
    C_DWORD,
    C_LDWORD,
    C_LLONG,
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_IN_REALPLAY_BY_DATA_TYPE,
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
    NET_OUT_REALPLAY_BY_DATA_TYPE,
    NET_DATA_CALL_BACK_INFO,
)


TS_CALLBACK_DATA_TYPE = 1002
H264_CALLBACK_DATA_TYPE = 1004
fRealDataEx = CB_FUNCTYPE(
    None, C_LLONG, C_DWORD, POINTER(c_byte), C_DWORD, C_LLONG, C_LDWORD
)


class NetSdkHlsManager:
    """Own all NetSDK live handles and their FFmpeg HLS consumers."""

    def __init__(self, output_root, event_log, host='192.168.100.229',
                 port=37777, username='admin', password=None, max_channels=8):
        self.output_root = Path(output_root)
        self.event_log = event_log
        self.host = host
        self.port = port
        self.username = username
        self.password = password or os.environ.get('DAHUA_PASSWORD')
        self.max_channels = max_channels
        self.adas_enabled = os.environ.get('ADAS_ENABLED', 'false').lower() == 'true'
        self.adas_channels = {
            int(value) for value in os.environ.get('ADAS_CHANNELS', '2').split(',')
            if value.strip()
        }
        self.adas_subtype = int(os.environ.get('ADAS_SUBTYPE', '0'))
        self.lock = threading.Lock()
        self.sessions = {}
        self.output_root.mkdir(parents=True, exist_ok=True)
        if not shutil.which('ffmpeg'):
            raise RuntimeError('ffmpeg is required for live HLS streaming')

        self.sdk = NetClient()
        self.disconnect_callback = fDisConnect(self._on_disconnect)
        self.reconnect_callback = fHaveReConnect(self._on_reconnect)
        self.sdk.InitEx(self.disconnect_callback)
        self.sdk.SetAutoReconnect(self.reconnect_callback)

    def start(self, channel, subtype=0):
        if not 1 <= channel <= self.max_channels:
            raise ValueError(f'channel must be between 1 and {self.max_channels}')
        if subtype not in (0, 1):
            raise ValueError('subtype must be 0 (main) or 1 (extra stream)')
        if self.adas_enabled and channel in self.adas_channels:
            subtype = self.adas_subtype

        with self.lock:
            current = self.sessions.get(channel)
            if current and current['play_id'] and current['process'].poll() is None:
                return self._public_state(current)

        if current:
            self.stop(channel)

        channel_dir = self.output_root / f'ch{channel}'
        channel_dir.mkdir(parents=True, exist_ok=True)
        for path in channel_dir.glob('stream*'):
            if path.is_file():
                path.unlink()

        log_path = channel_dir / 'ffmpeg.log'
        use_h264 = (self.adas_enabled and channel in self.adas_channels and
                    subtype == 0 and os.environ.get('ADAS_INPUT_FORMAT', 'mpegts') == 'h264')
        callback_data_type = H264_CALLBACK_DATA_TYPE if use_h264 else TS_CALLBACK_DATA_TYPE
        input_format = 'h264' if use_h264 else 'mpegts'
        # Each play handle gets a fresh diagnostic log. Keeping errors from an
        # older RTP implementation here makes a healthy NetSDK session appear
        # broken during field diagnosis.
        log_file = log_path.open('wb', buffering=0)
        playlist = channel_dir / 'stream.m3u8'
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-copyts', '-start_at_zero', '-fflags', '+igndts',
            '-f', input_format, '-i', 'pipe:0',
            '-map', '0:v:0', '-an', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-profile:v', 'baseline', '-pix_fmt', 'yuv420p',
            '-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
            '-f', 'hls', '-hls_time', '1', '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+append_list+independent_segments',
            '-hls_segment_filename', str(channel_dir / 'stream%05d.ts'),
            str(playlist),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        data_queue = queue.Queue(maxsize=64)
        stop_event = threading.Event()
        session = {
            'channel': channel,
            'subtype': subtype,
            'login_id': C_LLONG(),
            'play_id': C_LLONG(),
            'process': process,
            'log_file': log_file,
            'playlist': playlist,
            'output_dir': channel_dir,
            'queue': data_queue,
            'stop_event': stop_event,
            'started_at': time.time(),
            'bytes_received': 0,
            'dropped_chunks': 0,
            'callback_data_type': callback_data_type,
            'input_format': input_format,
            'adas': None,
        }

        def on_data(_handle, data_type, buffer, size, _param, _user):
            accepted_types = ({session['callback_data_type'], 0}
                              if session['input_format'] == 'h264'
                              else {session['callback_data_type']})
            if data_type not in accepted_types or not size or stop_event.is_set():
                return
            payload = bytes(cast(buffer, POINTER(c_ubyte * size)).contents)
            session['bytes_received'] += len(payload)
            if session['adas']:
                session['adas'].feed(payload, time.monotonic())
            try:
                data_queue.put_nowait(payload)
            except queue.Full:
                session['dropped_chunks'] += 1

        def on_data_timestamp(_handle, info_ptr, _user):
            info = info_ptr.contents
            if (info.dwDataType != session['callback_data_type'] or
                    not info.dwBufSize or stop_event.is_set()):
                return 0
            payload = bytes(cast(info.pBuffer, POINTER(c_ubyte * info.dwBufSize)).contents)
            received_at = time.monotonic()
            session['bytes_received'] += len(payload)
            pts_ms = int(info.stuTime.dwPTS)
            if session['adas']:
                session['adas'].feed(payload, received_at, pts_ms)
            try:
                data_queue.put_nowait(payload)
            except queue.Full:
                session['dropped_chunks'] += 1
            return 0

        session['data_callback'] = fRealDataEx(on_data)
        session['data_callback_timestamped'] = fDataCallBackEx(on_data_timestamp)
        session['writer_thread'] = threading.Thread(
            target=self._write_stream, args=(session,), daemon=True
        )
        session['writer_thread'].start()

        try:
            if self.adas_enabled and channel in self.adas_channels:
                # Avoid an NVENC session burst when the dashboard starts all channels.
                time.sleep(max(0, channel - 1) * 1.5)
                from camera_server.adas.pipeline_impl import AdasPipeline
                session['adas'] = AdasPipeline(channel, self.event_log, channel_dir)
                session['adas'].start()
            session['login_id'] = self._login()
            play_in = NET_IN_REALPLAY_BY_DATA_TYPE()
            play_in.dwSize = sizeof(NET_IN_REALPLAY_BY_DATA_TYPE)
            play_in.nChannelID = channel - 1
            play_in.hWnd = 0
            play_in.rType = (
                SDK_RealPlayType.Realplay if subtype == 0
                else SDK_RealPlayType.Realplay_1
            )
            play_in.cbRealDataEx = session['data_callback']
            # The timestamp callback on this firmware emits fragmented NAL
            # payloads for H264; use the elementary callback as the source of
            # truth for ADAS while retaining the TS timestamp callback for HLS.
            play_in.cbRealDataEx2 = None if use_h264 else session['data_callback_timestamped']
            play_in.emDataType = EM_REAL_DATA_TYPE.H264 if use_h264 else EM_REAL_DATA_TYPE.TS
            play_out = NET_OUT_REALPLAY_BY_DATA_TYPE()
            play_out.dwSize = sizeof(NET_OUT_REALPLAY_BY_DATA_TYPE)
            session['play_id'] = self.sdk.RealPlayByDataType(
                session['login_id'], play_in, play_out, 10000
            )
            if not session['play_id']:
                raise RuntimeError(f'NetSDK RealPlay failed: {self.sdk.GetLastErrorMessage()}')
            with self.lock:
                self.sessions[channel] = session
            self.event_log.add('success', f'Live ch{channel} NetSDK {input_format} ingest started')
            return self._public_state(session)
        except Exception:
            self._terminate(session)
            raise

    def stop(self, channel):
        if not 1 <= channel <= self.max_channels:
            raise ValueError(f'channel must be between 1 and {self.max_channels}')
        with self.lock:
            session = self.sessions.pop(channel, None)
        if not session:
            return False
        self._terminate(session)
        self._clean_media(session)
        self.event_log.add('success', f'Live ch{channel} stopped')
        return True

    def snapshot(self):
        with self.lock:
            sessions = list(self.sessions.values())
        return [self._public_state(session) for session in sessions]

    def shutdown(self):
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            self._terminate(session)
            self._clean_media(session)
        self.sdk.Cleanup()

    def _login(self):
        if not self.password:
            raise RuntimeError('DAHUA_PASSWORD is required to start Dahua media sessions')
        login_in = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
        login_in.dwSize = sizeof(NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY)
        login_in.szIP = self.host.encode()
        login_in.nPort = self.port
        login_in.szUserName = self.username.encode()
        login_in.szPassword = self.password.encode()
        login_in.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
        login_in.pCapParam = None
        login_out = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
        login_out.dwSize = sizeof(NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)
        login_id, _device, error = self.sdk.LoginWithHighLevelSecurity(login_in, login_out)
        if not login_id:
            raise RuntimeError(f'NetSDK login failed: {error}')
        return login_id

    @staticmethod
    def _write_stream(session):
        process = session['process']
        try:
            while not session['stop_event'].is_set():
                try:
                    payload = session['queue'].get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                process.stdin.write(payload)
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _terminate(self, session):
        session['stop_event'].set()
        if session.get('adas'):
            session['adas'].stop()
        if session.get('play_id'):
            self.sdk.StopRealPlayEx(session['play_id'])
            session['play_id'] = C_LLONG()
        if session.get('login_id'):
            self.sdk.Logout(session['login_id'])
            session['login_id'] = C_LLONG()
        process = session['process']
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        session['writer_thread'].join(timeout=2)
        session['log_file'].close()

    @staticmethod
    def _public_state(session):
        playlist = session['playlist']
        ready = playlist.is_file() and playlist.stat().st_size > 0
        process_alive = session['process'].poll() is None
        state = {
            'channel': session['channel'],
            'subtype': session['subtype'],
            'transport': f'dahua-netsdk-{session["input_format"]}',
            'status': 'ready' if ready else ('starting' if process_alive else 'error'),
            'playlist_url': f'/live/ch{session["channel"]}/stream.m3u8',
            'uptime_seconds': round(time.time() - session['started_at'], 1),
            'bytes_received': session['bytes_received'],
            'dropped_chunks': session['dropped_chunks'],
        }
        if session.get('adas'):
            state['adas'] = session['adas'].snapshot()
        return state

    @staticmethod
    def _clean_media(session):
        for path in session['output_dir'].glob('stream*'):
            if path.is_file():
                path.unlink()

    def _on_disconnect(self, _login_id, _ip, _port, _user):
        self.event_log.add('error', 'Dahua NetSDK disconnected')

    def _on_reconnect(self, _login_id, _ip, _port, _user):
        self.event_log.add('success', 'Dahua NetSDK reconnected')
