"""Bounded low-latency ADAS detection and annotated WebRTC publisher."""

from __future__ import annotations

import collections
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlsplit, urlunsplit

from .scheduler import InferenceJob, get_shared_scheduler

FRAME_WIDTH = 320
FRAME_HEIGHT = 192
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3
TARGET_CLASSES = {0, 2, 3, 5, 7}  # COCO: person, car, motorcycle, bus, truck


class RollingLatency:
    def __init__(self, capacity=2048):
        self.values = collections.deque(maxlen=capacity)
        self.lock = threading.Lock()

    def add(self, value_ms):
        with self.lock:
            self.values.append(float(value_ms))

    def snapshot(self):
        with self.lock:
            values = sorted(self.values)
        if not values:
            return {'count': 0, 'p50_ms': None, 'p95_ms': None, 'p99_ms': None}

        def percentile(fraction):
            return round(values[min(len(values) - 1, int((len(values) - 1) * fraction))], 2)

        return {
            'count': len(values),
            'p50_ms': percentile(0.50),
            'p95_ms': percentile(0.95),
            'p99_ms': percentile(0.99),
        }


class LatestFrameSlot:
    """Capacity-one latest-state handoff; replacement is intentional."""

    def __init__(self):
        self.condition = threading.Condition()
        self.item = None
        self.sequence = 0
        self.replaced = 0
        self.closed = False

    def put(self, frame, received_at, source_pts_ms=None, source_capture_at=None):
        with self.condition:
            if self.item is not None:
                self.replaced += 1
            self.sequence += 1
            self.item = (self.sequence, frame, received_at, source_pts_ms, source_capture_at)
            self.condition.notify()

    def get(self, timeout=0.5):
        with self.condition:
            if self.item is None and not self.closed:
                self.condition.wait(timeout)
            item = self.item
            self.item = None
            return item

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class AdasPipeline:
    _model_cache = {}
    _model_cache_lock = threading.Lock()
    def __init__(self, channel, event_log, output_dir):
        self.channel = channel
        self.event_log = event_log
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        default_model = Path(__file__).resolve().parents[2] / 'models' / 'yolov8n.pt'
        self.model_path = Path(os.environ.get('ADAS_MODEL_PATH', str(default_model)))
        configured_rtsp = os.environ.get('MEDIAMTX_PUBLISH_URL')
        if configured_rtsp:
            parts = urlsplit(configured_rtsp)
            self.mediamtx_rtsp = urlunsplit((
                parts.scheme, parts.netloc, f'/adas-ch{channel}', parts.query, parts.fragment
            ))
        else:
            self.mediamtx_rtsp = f'rtsp://127.0.0.1:8554/adas-ch{channel}'
        self.webrtc_url = os.environ.get(
            'MEDIAMTX_WEBRTC_BASE', 'http://192.168.100.108:8889'
        ).rstrip('/') + f'/adas-ch{channel}'
        self.stop_event = threading.Event()
        self.encoded_queue = queue.Queue(maxsize=64)
        self.publish_queue = queue.Queue(maxsize=1)
        self.pending_source_timestamps = collections.deque()
        self.source_pts_lock = threading.Lock()
        self.source_clock_offset = None
        self.latest_frame = LatestFrameSlot()
        self.lock = threading.Lock()
        self.started_at = time.monotonic()
        # Convert the monotonic packet-receive clock to wall-clock time for a
        # visual comparison with the camera's OSD timestamp. This is receive
        # time, not a claim of source-frame timestamp accuracy.
        self.wall_clock_offset = time.time() - time.monotonic()
        self.last_sdk_received_at = None
        self.frames_decoded = 0
        self.frames_inferred = 0
        self.frames_dropped_stale = 0
        self.encoded_chunks_dropped = 0
        self.decode_errors = 0
        self.last_error = None
        self.status = 'starting'
        self.decode_to_detection_latency = RollingLatency()
        self.decode_to_publish_latency = RollingLatency()
        self.dahua_to_detection_latency = RollingLatency()
        self.dahua_to_publish_latency = RollingLatency()
        self.warning_enabled = os.environ.get('ADAS_WARNING_ENABLED', 'true').lower() == 'true'
        self.publish_fps = max(5, int(os.environ.get('ADAS_PUBLISH_FPS', '10')))
        self.warning_roi = self._parse_roi(os.environ.get('ADAS_WARNING_ROI', '0.15,0.35,0.85,0.98'))
        self.warning_debounce_frames = max(1, int(os.environ.get('ADAS_WARNING_DEBOUNCE_FRAMES', '3')))
        self.warning_cooldown_ms = max(0, int(os.environ.get('ADAS_WARNING_COOLDOWN_MS', '1000')))
        self.warning_streak = 0
        self.warning_total = 0
        self.last_warning_at = None
        self.latest_warning = None
        self.model = None
        self.scheduler = None
        self.decoder = None
        self.publisher = None
        self.process_logs: list[BinaryIO] = []
        self.threads = []

    def start(self):
        self._validate_runtime()
        self.scheduler = get_shared_scheduler(self.model_path)
        self.model = self.scheduler.model
        self.decoder = self._start_decoder()
        self.publisher = self._start_publisher()
        self.threads = [
            threading.Thread(target=self._encoded_writer, daemon=True),
            threading.Thread(target=self._decoded_reader, daemon=True),
            threading.Thread(target=self._inference_submitter, daemon=True),
            threading.Thread(target=self._publisher_writer, daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        return self.snapshot()

    def feed(self, payload, received_at=None, source_pts_ms=None):
        if self.stop_event.is_set():
            return
        timestamp = received_at or time.monotonic()
        self.last_sdk_received_at = timestamp
        try:
            self.encoded_queue.put_nowait((payload, timestamp, source_pts_ms))
        except queue.Full:
            self.encoded_chunks_dropped += 1

    def stop(self):
        self.stop_event.set()
        self.latest_frame.close()
        for process in (self.decoder, self.publisher):
            if not process:
                continue
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        for thread in self.threads:
            thread.join(timeout=2)
        for log_file in self.process_logs:
            log_file.close()
        self.process_logs.clear()

    def snapshot(self):
        now = time.monotonic()
        age_ms = None if self.last_sdk_received_at is None else round(
            (now - self.last_sdk_received_at) * 1000, 2
        )
        decoder_alive = bool(self.decoder and self.decoder.poll() is None)
        publisher_alive = bool(self.publisher and self.publisher.poll() is None)
        status = self.status
        if self.last_error or (self.decoder and not decoder_alive):
            status = 'unavailable'
        elif self.frames_inferred and age_ms is not None and age_ms <= 150:
            status = 'healthy'
        elif self.frames_decoded:
            status = 'warming_up'
        return {
            'channel': self.channel,
            'status': status,
            'model': self.model_path.name,
            'webrtc_url': self.webrtc_url,
            'sdk_packet_age_ms': age_ms,
            'frames_decoded': self.frames_decoded,
            'frames_inferred': self.frames_inferred,
            'frames_dropped_stale': self.frames_dropped_stale,
            'latest_frame_replaced': self.latest_frame.replaced,
            'encoded_chunks_dropped': self.encoded_chunks_dropped,
            'decode_errors': self.decode_errors,
            'decoder_alive': decoder_alive,
            'publisher_alive': publisher_alive,
            'decode_to_detection': self.decode_to_detection_latency.snapshot(),
            'decode_to_publish': self.decode_to_publish_latency.snapshot(),
            'dahua_to_detection': self.dahua_to_detection_latency.snapshot(),
            'dahua_to_publish': self.dahua_to_publish_latency.snapshot(),
            'dahua_timestamp': {
                'verified': self.source_clock_offset is not None,
                'kind': 'NetSDK NET_DATA_CALL_BACK_INFO.stuTime.dwPTS, stream-relative',
                'reason': None if self.source_clock_offset is not None else 'NetSDK TS callback did not provide frame timestamp metadata',
            },
            'warning_policy': {
                'enabled': self.warning_enabled,
                'roi': self.warning_roi,
                'debounce_frames': self.warning_debounce_frames,
                'cooldown_ms': self.warning_cooldown_ms,
                'total': self.warning_total,
                'latest': self.latest_warning,
            },
            'sdk_to_detection': {
                'verified': False,
                'reason': 'encoded callback chunks are not yet correlated to decoded frame PTS',
            },
            'last_error': self.last_error,
            'uptime_seconds': round(now - self.started_at, 1),
        }

    def _validate_runtime(self):
        if not self.model_path.is_file():
            if self.model_path.suffix == '.engine':
                raise RuntimeError(f'TensorRT engine not found: {self.model_path}')
            # .pt model will be loaded by Ultralytics; no engine file needed
        try:
            import cv2 as _cv2  # noqa: F401
            import tensorrt as _tensorrt  # noqa: F401
            from ultralytics import YOLO as _YOLO  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(f'ADAS TensorRT dependency missing: {exc}') from exc
        self.status = 'warming_up'

    @staticmethod
    def _parse_roi(value):
        try:
            parts = [float(item.strip()) for item in value.split(',')]
            if len(parts) != 4 or not all(0.0 <= item <= 1.0 for item in parts):
                raise ValueError
            x1, y1, x2, y2 = parts
            if x2 <= x1 or y2 <= y1:
                raise ValueError
            return [round(item, 4) for item in parts]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                'ADAS_WARNING_ROI must be normalized x1,y1,x2,y2'
            ) from exc

    def _start_decoder(self):
        log_file = (self.output_dir / 'adas-decoder.log').open('wb', buffering=0)
        decoder_codec = os.environ.get('ADAS_DECODER_CODEC', 'h264_cuvid')
        input_format = os.environ.get('ADAS_INPUT_FORMAT', 'mpegts')
        decoder_prefix = ['-hwaccel', 'cuda', '-c:v', decoder_codec] if decoder_codec == 'h264_cuvid' else ['-c:v', decoder_codec]
        process = subprocess.Popen([
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            *decoder_prefix,
            '-fflags', 'nobuffer', '-flags', 'low_delay',
            '-f', input_format, '-i', 'pipe:0', '-an',
            '-vf', f'scale={FRAME_WIDTH}:{FRAME_HEIGHT}',
            '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1',
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file,
           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.process_logs.append(log_file)
        return process

    def _start_publisher(self):
        log_file = (self.output_dir / 'adas-publisher.log').open('wb', buffering=0)
        encoder = os.environ.get('ADAS_PUBLISH_CODEC', 'libx264')
        encoder_args = ['-c:v', encoder]
        if encoder == 'libx264':
            encoder_args += ['-preset', 'ultrafast', '-tune', 'zerolatency']
        else:
            encoder_args += ['-preset', 'p1', '-tune', 'ull']
        process = subprocess.Popen([
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-f', 'rawvideo', '-pix_fmt', 'bgr24',
            '-s', f'{FRAME_WIDTH}x{FRAME_HEIGHT}', '-r', str(self.publish_fps), '-i', 'pipe:0',
            '-an', *encoder_args,
            '-pix_fmt', 'yuv420p', '-profile:v', 'main',
            '-bf', '0', '-g', '12', '-forced-idr', '1',
            '-f', 'rtsp', '-rtsp_transport', 'tcp', self.mediamtx_rtsp,
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log_file,
           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.process_logs.append(log_file)
        return process

    def _encoded_writer(self):
        decoder = self.decoder
        if decoder is None or decoder.stdin is None:
            self._fail('NVDEC process has no writable stdin')
            return
        try:
            while not self.stop_event.is_set():
                try:
                    payload, received_at, source_pts_ms = self.encoded_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                with self.source_pts_lock:
                    self.pending_source_timestamps.append((received_at, source_pts_ms))
                    if source_pts_ms is not None and self.source_clock_offset is None:
                        self.source_clock_offset = received_at - source_pts_ms / 1000.0
                decoder.stdin.write(payload)
                decoder.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._fail(f'NVDEC input failed: {exc}')

    def _publisher_writer(self):
        publisher = self.publisher
        if publisher is None or publisher.stdin is None:
            self._fail('WebRTC publisher has no writable stdin')
            return
        try:
            while not self.stop_event.is_set():
                try:
                    frame, received_at, source_pts_ms, source_capture_at = self.publish_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if publisher.poll() is not None:
                    self._fail('WebRTC publisher exited before frame write')
                    return
                try:
                    publisher.stdin.write(frame.tobytes())
                    publisher.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    self._fail(f'WebRTC publisher input failed: {exc}')
                    return
                self.decode_to_publish_latency.add((time.monotonic() - received_at) * 1000)
                if source_pts_ms is not None and source_capture_at is not None:
                    self.dahua_to_publish_latency.add((time.monotonic() - source_capture_at) * 1000)
        except Exception as exc:
            self._fail(f'Publisher worker failed: {exc}')

    def _decoded_reader(self):
        import numpy as np
        decoder = self.decoder
        if decoder is None or decoder.stdout is None:
            self._fail('NVDEC process has no readable stdout')
            return
        try:
            while not self.stop_event.is_set():
                raw = decoder.stdout.read(FRAME_BYTES)
                if len(raw) != FRAME_BYTES:
                    if not self.stop_event.is_set():
                        self.decode_errors += 1
                        self._fail('NVDEC output ended before a complete frame')
                    return
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (FRAME_HEIGHT, FRAME_WIDTH, 3)
                ).copy()
                self.frames_decoded += 1
                with self.source_pts_lock:
                    source_received_at, source_pts_ms = (
                        self.pending_source_timestamps.popleft()
                        if self.pending_source_timestamps else (time.monotonic(), None)
                    )
                source_capture_at = None
                if source_pts_ms is not None and self.source_clock_offset is not None:
                    source_capture_at = self.source_clock_offset + source_pts_ms / 1000.0
                self.latest_frame.put(frame, time.monotonic(), source_pts_ms, source_capture_at)
        except (OSError, ValueError) as exc:
            self.decode_errors += 1
            self._fail(f'NVDEC output failed: {exc}')

    def _inference_submitter(self):
        scheduler = self.scheduler
        if scheduler is None:
            self._fail('inference scheduler is unavailable')
            return
        while not self.stop_event.is_set():
            item = self.latest_frame.get()
            if item is None:
                continue
            sequence, frame, received_at, source_pts_ms, source_capture_at = item
            if (time.monotonic() - received_at) * 1000 > 150:
                self.frames_dropped_stale += 1
                continue
            scheduler.submit_latest(InferenceJob(
                channel=self.channel, frame=(frame, source_pts_ms, source_capture_at),
                sequence=sequence, received_at=received_at, callback=self._run_inference,
            ))

    def _run_inference(self, job):
        import cv2
        if self.stop_event.is_set():
            return
        frame, source_pts_ms, source_capture_at = job.frame
        received_at = job.received_at
        try:
            age_ms = (time.monotonic() - received_at) * 1000
            if age_ms > 150:
                self.frames_dropped_stale += 1
                return
            if self.model is None:
                raise RuntimeError('ADAS model is unavailable')
            results = cast(Any, self.model.predict(
                frame, imgsz=(FRAME_HEIGHT, FRAME_WIDTH), batch=1, conf=0.25,
                iou=0.45, classes=sorted(TARGET_CLASSES), verbose=False))
            result = results[0]
            warning_candidate: dict[str, Any] | None = None
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                confidence, class_id = float(box.conf[0]), int(box.cls[0])
                name = result.names.get(class_id, str(class_id))
                cx, cy = (x1 + x2) / 2 / FRAME_WIDTH, (y1 + y2) / 2 / FRAME_HEIGHT
                rx1, ry1, rx2, ry2 = self.warning_roi
                if self.warning_enabled and rx1 <= cx <= rx2 and ry1 <= cy <= ry2 and (warning_candidate is None or confidence > warning_candidate['confidence']):
                    warning_candidate = {'class': name, 'class_id': class_id, 'confidence': round(confidence, 4), 'bbox': [x1, y1, x2, y2]}
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(frame, f'{name} {confidence:.2f}', (x1, max(16, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            latency_ms = (time.monotonic() - received_at) * 1000
            if source_pts_ms is not None and source_capture_at is not None:
                self.dahua_to_detection_latency.add((time.monotonic() - source_capture_at) * 1000)
            self._update_warning(warning_candidate, received_at, latency_ms)
            self.decode_to_detection_latency.add(latency_ms)
            self.frames_inferred += 1
            cv2.putText(frame, f'decode->detect {latency_ms:.0f} ms', (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.putText(frame, f'dropped {self.frames_dropped_stale}', (8, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            ts_label = 'TS recv ' + datetime.fromtimestamp(received_at + self.wall_clock_offset).strftime('%H:%M:%S.%f')[:-3]
            if source_pts_ms is not None:
                ts_label += f' PTS {source_pts_ms} ms'
            (label_width, _), _ = cv2.getTextSize(ts_label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.putText(frame, ts_label, (FRAME_WIDTH - label_width - 8, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            try:
                self.publish_queue.put_nowait((frame, received_at, source_pts_ms, source_capture_at))
            except queue.Full:
                self.encoded_chunks_dropped += 1
        except (BrokenPipeError, OSError) as exc:
            self._fail(f'WebRTC publisher input failed: {exc}')
        except Exception as exc:
            self._fail(f'Inference failed: {exc}')

    def _update_warning(self, candidate, received_at, latency_ms):
        if not self.warning_enabled:
            return
        if candidate is None:
            self.warning_streak = 0
            return
        self.warning_streak += 1
        if self.warning_streak < self.warning_debounce_frames:
            return
        now = time.monotonic()
        if self.last_warning_at is not None and (now - self.last_warning_at) * 1000 < self.warning_cooldown_ms:
            return
        self.last_warning_at = now
        event = {
            'event_id': uuid.uuid4().hex,
            'type': 'adas_level0_warning',
            'channel': self.channel,
            'class': candidate['class'],
            'class_id': candidate['class_id'],
            'confidence': candidate['confidence'],
            'bbox': candidate['bbox'],
            'roi': self.warning_roi,
            'capture_monotonic': round(received_at, 6),
            'decision_monotonic': round(now, 6),
            'decision_latency_ms': round(latency_ms, 2),
            'model': self.model_path.name,
        }
        self.warning_total += 1
        self.latest_warning = event
        self.event_log.add('warning', f'ADAS ch{self.channel}: {candidate["class"]} in ROI ({latency_ms:.0f} ms)')

    def _fail(self, message):
        with self.lock:
            if self.last_error is None:
                self.last_error = message
                self.status = 'unavailable'
                self.event_log.add('error', f'ADAS ch{self.channel}: {message}')
