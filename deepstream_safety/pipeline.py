#!/usr/bin/env python3
"""Standalone DeepStream Safety pipeline.

RTSP input -> person detector/tracker -> function-specific analysis -> NVOSD
-> RTSP output. Smoking behavior is a state attached to a person bbox;
fire/smoke are camera-level environmental detections. The pipeline does not
import Frigate or read any Frigate configuration.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import logging
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config import load_config
from evidence import EvidenceStore

DEEPSTREAM_ROOT = os.environ.get("DEEPSTREAM_ROOT", "/opt/nvidia/deepstream/deepstream-7.1")
os.environ["GIO_USE_PROXY"] = "0"
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
_plugin_path = os.path.join(DEEPSTREAM_ROOT, "lib", "gst-plugins")
_library_paths = [
    os.path.join(DEEPSTREAM_ROOT, "lib"),
    _plugin_path,
    "/usr/local/lib/python3.10/dist-packages/tensorrt_libs",
]
os.environ["GST_PLUGIN_PATH"] = os.pathsep.join(
    _library_paths[1:] + ([os.environ["GST_PLUGIN_PATH"]] if os.environ.get("GST_PLUGIN_PATH") else [])
)
os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
    _library_paths + ([os.environ["LD_LIBRARY_PATH"]] if os.environ.get("LD_LIBRARY_PATH") else [])
)

import gi  # noqa: E402
import numpy as np  # noqa: E402
import pyds  # noqa: E402
import zmq  # noqa: E402
from events import SafetyDetection, SafetyEventStore  # noqa: E402
from face_engine import FaceRecognitionEngine  # noqa: E402
from fire_smoke_engine import FireSmokeEngine  # noqa: E402
from fire_smoke_events import FireSmokeEventStore  # noqa: E402
from notifications import NotificationService  # noqa: E402
from recognition import RecognitionCore, TrackKey  # noqa: E402
from smoking_behavior_engine import SmokingBehaviorEngine  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

LOG = logging.getLogger("deepstream-safety")


def make_element(factory: str, name: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element is unavailable: {factory}")
    return element


def iou(left: np.ndarray, right: np.ndarray) -> float:
    x1 = max(float(left[0]), float(right[0]))
    y1 = max(float(left[1]), float(right[1]))
    x2 = min(float(left[2]), float(right[2]))
    y2 = min(float(left[3]), float(right[3]))
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_left = max(0.0, float(left[2] - left[0])) * max(0.0, float(left[3] - left[1]))
    area_right = max(0.0, float(right[2] - right[0])) * max(0.0, float(right[3] - right[1]))
    union = area_left + area_right - intersection
    return intersection / union if union > 0 else 0.0


def frigate_track_distance(detection: np.ndarray, estimate: np.ndarray) -> float:
    """Normalize position and size changes like Frigate's Norfair tracker."""
    estimate_dim = np.diff(estimate.reshape(2, 2), axis=0).flatten()
    detection_dim = np.diff(detection.reshape(2, 2), axis=0).flatten()
    if (
        not np.all(np.isfinite(estimate_dim))
        or not np.all(np.isfinite(detection_dim))
        or np.any(estimate_dim <= 0)
        or np.any(detection_dim <= 0)
    ):
        return float("inf")

    detection_position = np.array(
        [(detection[0] + detection[2]) / 2.0, detection[3]], dtype=np.float32
    )
    estimate_position = np.array(
        [(estimate[0] + estimate[2]) / 2.0, estimate[3]], dtype=np.float32
    )
    position_delta = detection_position - estimate_position
    position_delta[0] /= estimate_dim[0]
    position_delta[1] /= estimate_dim[1]
    widths = np.sort([estimate_dim[0], detection_dim[0]])
    heights = np.sort([estimate_dim[1], detection_dim[1]])
    change = np.append(
        position_delta,
        np.array([widths[1] / widths[0] - 1.0, heights[1] / heights[0] - 1.0]),
    )
    return float(np.linalg.norm(change))


def opposite_frame_edge_transition(
    previous: np.ndarray, current: np.ndarray, width: float, height: float
) -> bool:
    """Reject a stale prediction that wraps into a new passage at another edge."""
    x_margin = width * 0.025
    y_margin = height * 0.025
    previous_left = previous[0] <= x_margin
    previous_top = previous[1] <= y_margin
    previous_right = previous[2] >= width - x_margin
    previous_bottom = previous[3] >= height - y_margin
    current_left = current[0] <= x_margin
    current_top = current[1] <= y_margin
    current_right = current[2] >= width - x_margin
    current_bottom = current[3] >= height - y_margin
    return (
        (previous_left and current_right)
        or (previous_right and current_left)
        or (previous_top and current_bottom)
        or (previous_bottom and current_top)
    )


def nms(boxes: list[np.ndarray], threshold: float) -> list[np.ndarray]:
    selected: list[np.ndarray] = []
    for box in sorted(boxes, key=lambda item: float(item[4]), reverse=True):
        if all(iou(box, other) <= threshold for other in selected):
            selected.append(box)
    return selected


class SafetyPipeline:
    def __init__(self, config: dict[str, Any], config_path: Path, run_id: str) -> None:
        self.config = config
        self.config_path = config_path
        self.run_id = run_id
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.Pipeline.new("deepstream-safety")
        if self.pipeline is None:
            raise RuntimeError("Unable to create GStreamer pipeline")
        self.depay: Gst.Element | None = None
        self.person_infer: Gst.Element | None = None
        self.frame_probe_id: int | None = None
        self.started_at = time.monotonic()
        runtime = config.get("runtime", {}) or {}
        self.status_dir = Path(str(runtime.get("status_directory", "/opt/camera-deepstream/status")))
        self.status_path = self.status_dir / f"{config.get('input', {}).get('camera', 'camera')}.json"
        self.last_frame_at: float | None = None
        functions = config.get("functions", {}) or {}
        self.smoking_behavior_enabled = bool(functions.get("smoking_behavior", False))
        self.fire_smoke_enabled = bool(functions.get("fire_smoke", False))
        self.trace_enabled = bool(functions.get("trace", True))
        LOG.info(
            "function topology: camera=%s face_recognition=%s smoking_behavior=%s fire_smoke=%s",
            config.get("input", {}).get("camera", "unknown"),
            bool(functions.get("face_recognition", False)),
            self.smoking_behavior_enabled,
            self.fire_smoke_enabled,
        )
        self.frame_count = 0
        self.person_frame_count = 0
        self.last_person_count = 0
        self.last_bbox_count = 0
        self.last_fire_smoke_count = 0
        self.person_tensor_logged = False
        self.person_score_logged = False
        self.evidence = EvidenceStore(config, run_id)
        self.notifications = NotificationService(config, self.evidence.root, run_id)
        self.event_store = SafetyEventStore(config, self.evidence)
        self.fire_smoke_events = FireSmokeEventStore(config, self.evidence)
        self._face_event_ids: dict[int, str] = {}
        self._smoking_by_track: dict[int, Any] = {}
        self.mock_publisher: subprocess.Popen | None = None
        self.recognition = RecognitionCore(config)
        self.face_engine = FaceRecognitionEngine(config, self._on_face_trace)
        self.smoking_behavior_engine = (
            SmokingBehaviorEngine(config) if self.smoking_behavior_enabled else None
        )
        self.fire_smoke_engine = FireSmokeEngine(config) if self.fire_smoke_enabled else None
        self.recognition_last_frame: dict[TrackKey, int] = {}
        self.last_event_transition: str | None = None
        self._person_tracks: dict[int, dict[str, Any]] = {}
        self._next_person_track_id = 1
        tracking_config = config.get("person", {}).get("tracking", {})
        self._person_max_disappeared = max(
            2,
            int(
                tracking_config.get(
                    "max_disappeared", max(2, int(config["input"].get("fps", 5)) * 2)
                )
            ),
        )
        self._person_reacquire_max_disappeared = max(
            0, int(tracking_config.get("reacquire_max_disappeared", 1))
        )
        self._person_distance_threshold = float(
            tracking_config.get("distance_threshold", 2.5)
        )
        self._person_bbox_smoothing_alpha = min(
            1.0,
            max(0.05, float(tracking_config.get("bbox_smoothing_alpha", 0.30))),
        )
        self.last_behavior_error: str | None = None
        self.analysis_enabled = (
            self.face_engine.enabled
            or self.smoking_behavior_enabled
            or self.fire_smoke_enabled
        )
        self._analysis_queue: queue.Queue = queue.Queue(maxsize=1)
        self._analysis_stop = threading.Event()
        self._analysis_thread: threading.Thread | None = None
        self._analysis_lock = threading.RLock()
        self._analysis_detections: list[Any] = []
        self._analysis_fire_smoke: list[Any] = []
        self._analysis_transitions: list[Any] = []
        self._analysis_last_transition: str | None = None
        self._analysis_frame_num: int | None = None
        self._analysis_updated_at: float | None = None
        input_fps = max(1.0, float(config.get("input", {}).get("fps", 5)))
        self._analysis_max_age_frames = max(2, int(round(input_fps * 0.75)))
        analysis_intervals = [
            float(engine.interval_seconds)
            for engine in (self.smoking_behavior_engine, self.fire_smoke_engine)
            if engine is not None
        ]
        if self.face_engine.enabled:
            analysis_intervals.append(
                self.face_engine.recognition_scheduler.interval_seconds
            )
        self._analysis_interval_seconds = min(analysis_intervals, default=0.5)
        self._next_analysis_at = 0.0
        self._analysis_error: str | None = None
        self._analysis_probe_count = 0
        self._analysis_due_count = 0
        self._analysis_enqueued_count = 0
        self._analysis_processed_count = 0
        self._analysis_last_enqueued_frame: int | None = None
        self._analysis_last_processed_frame: int | None = None
        self._face_tracks: dict[int, dict[str, Any]] = {}
        self._latest_person_frame_num: int | None = None
        self._latest_person_rois: list[tuple[int, float, float, float, float]] = []
        self._latest_person_updated_at: float | None = None
        self._last_metadata_person_count = 0
        self._last_behavior_person_count = 0
        self._metadata_write_at = 0.0
        self.metadata_path = self.status_dir / f"{config.get('input', {}).get('camera', 'camera')}.metadata.json"
        self.socket = zmq.Context.instance().socket(zmq.PUB)
        self.socket.bind(config["metadata"]["zmq_pub_url"])
        self.person_infer_config = self._write_person_infer_config()
        self._build()
        if self.analysis_enabled:
            self._analysis_thread = threading.Thread(
                target=self._analysis_loop,
                name=f"analysis-{config['input'].get('camera', 'camera')}",
                daemon=True,
            )
            self._analysis_thread.start()

    def _on_face_trace(
        self, track_id: int, data: dict[str, Any], frame: np.ndarray | None
    ) -> None:
        event_name = str(data.get("event", "update"))
        event_id = self._face_event_ids.get(track_id)
        if event_name == "track_start":
            event_id = self.evidence.start_event(
                event_id=(
                    f"face-{self.run_id}-{self.config['input']['camera']}"
                    f"-{self.evidence.worker_epoch}-{track_id}"
                ),
                function="face_recognition",
                classification="pending",
                camera_id=str(self.config["input"]["camera"]),
                person_track_id=track_id,
                pending=True,
            )
            self._face_event_ids[track_id] = event_id
            return
        if event_id is None:
            event_id = self.evidence.start_event(
                event_id=(
                    f"face-{self.run_id}-{self.config['input']['camera']}"
                    f"-{self.evidence.worker_epoch}-{track_id}"
                ),
                function="face_recognition",
                classification="pending",
                camera_id=str(self.config["input"]["camera"]),
                person_track_id=track_id,
                pending=True,
            )
            self._face_event_ids[track_id] = event_id
        if event_name == "track_end":
            final_name = str(data.get("name") or "unknown")
            self.evidence.finish_event(
                event_id,
                classification="recognized" if final_name != "unknown" else "unrecognized",
                identity=None if final_name == "unknown" else final_name,
                payload=data,
                frame=frame,
                frame_number=int(data.get("frame", -1)),
                bbox=tuple(data.get("person_bbox", [])) if data.get("person_bbox") else None,
                score=float(data.get("score", 0.0)),
            )
            self._notify_event(event_id, "END")
            self._face_event_ids.pop(track_id, None)
            return
        stable_name = str(data.get("stable_result") or "unknown")
        self.evidence.record(
            event_id,
            "UPDATE",
            {**data, "identity": None if stable_name == "unknown" else stable_name},
            frame=frame,
            frame_number=int(data.get("frame", -1)),
            bbox=tuple(data.get("person_bbox", [])) if data.get("person_bbox") else None,
            score=float(data.get("stable_score", data.get("score", 0.0))),
        )

    def _notify_event(self, event_id: str, lifecycle: str) -> None:
        """Queue only an artifact-backed lifecycle notification."""
        self.notifications.notify_event(
            event_id,
            lifecycle,
            self.evidence.event_directory(event_id),
        )

    def _notify_transitions(self, transitions: list[Any]) -> None:
        for transition in transitions:
            operation = str(getattr(transition, "operation", ""))
            if operation in {"START", "END"}:
                self._notify_event(str(transition.event_id), operation)

    def _write_person_infer_config(self) -> str:
        model = self.config["person"]
        model_path = os.path.abspath(model["onnx_path"])
        engine_path = str(model.get("engine_path", "/opt/camera-deepstream/models/person-yolov9-t320.engine"))
        model_source = (
            f"model-engine-file={engine_path}"
            if Path(engine_path).is_file()
            else f"onnx-file={model_path}\nmodel-engine-file={engine_path}"
        )
        content = f"""[property]
gpu-id=0
{model_source}
labelfile-path=/tmp/deepstream-safety-person-labels.txt
batch-size=1
network-mode=2
network-type=100
model-color-format=0
net-scale-factor=0.00392156862745098
num-detected-classes=80
infer-dims=3;{int(model['input_width'])};{int(model['input_height'])}
interval=0
gie-unique-id=2
process-mode=1
output-tensor-meta=1
maintain-aspect-ratio=0
"""
        Path("/tmp/deepstream-safety-person-labels.txt").write_text("person\n", encoding="utf-8")
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix="deepstream-safety-person-infer-", suffix=".txt", delete=False
        )
        with handle:
            handle.write(content)
        LOG.info("person nvinfer config: %s", handle.name)
        return handle.name

    def _build(self) -> None:
        input_cfg = self.config["input"]
        output_cfg = self.config["output"]

        source = make_element("rtspsrc", "rtsp-source")
        source.set_property("location", input_cfg["rtsp_url"])
        if input_cfg.get("rtsp_username"):
            source.set_property("user-id", input_cfg["rtsp_username"])
        if input_cfg.get("rtsp_password"):
            source.set_property("user-pw", input_cfg["rtsp_password"])
        source.set_property("latency", int(input_cfg["latency_ms"]))
        source.set_property("protocols", 4)
        codec = str(input_cfg.get("codec", "h264")).lower()
        if codec in {"h265", "hevc"}:
            depay = make_element("rtph265depay", "rtp-h265-depay")
            parser = make_element("h265parse", "input-h265-parse")
        else:
            depay = make_element("rtph264depay", "rtp-h264-depay")
            parser = make_element("h264parse", "input-h264-parse")
        self.depay = depay
        decoder = make_element("nvv4l2decoder", "input-decoder")
        mux = make_element("nvstreammux", "stream-muxer")
        mux.set_property("batch-size", 1)
        mux.set_property("width", int(input_cfg["width"]))
        mux.set_property("height", int(input_cfg["height"]))
        mux.set_property("live-source", 1)
        mux.set_property("batched-push-timeout", 40000)

        self.person_infer = make_element("nvinfer", "person-inference")
        self.person_infer.set_property("config-file-path", self.person_infer_config)
        analysis_tee = make_element("tee", "analysis-tee")
        analysis_queue = make_element("queue", "analysis-queue")
        analysis_queue.set_property("max-size-buffers", 1)
        analysis_queue.set_property("max-size-bytes", 0)
        analysis_queue.set_property("max-size-time", 0)
        analysis_queue.set_property("leaky", 2)
        output_input_queue = make_element("queue", "output-input-queue")
        output_input_queue.set_property("max-size-buffers", 2)
        output_input_queue.set_property("max-size-bytes", 0)
        output_input_queue.set_property("max-size-time", 0)
        output_input_queue.set_property("leaky", 2)
        face_cpu_convert = make_element("nvvideoconvert", "face-cpu-convert")
        face_cpu_caps = make_element("capsfilter", "face-cpu-caps")
        face_cpu_caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGRx"))
        analysis_sink = make_element("fakesink", "analysis-sink")
        analysis_sink.set_property("sync", False)
        analysis_sink.set_property("async", False)
        convert_before_osd = make_element("nvvideoconvert", "convert-before-osd")
        face_rgba_caps = make_element("capsfilter", "face-rgba-caps")
        face_rgba_caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=RGBA"))
        osd = make_element("nvdsosd", "bbox-osd")
        osd.set_property("display-bbox", True)
        osd.set_property("display-text", True)
        convert_after_osd = make_element("nvvideoconvert", "convert-after-osd")
        output_rate = make_element("videorate", "output-rate")
        output_rate.set_property("max-duplication-time", 100_000_000)
        output_tee = make_element("tee", "output-tee")
        output_queue = make_element("queue", "output-queue")
        try:
            encoder = make_element("nvv4l2h264enc", "output-encoder")
            encoder.set_property("bitrate", 4_000_000)
            # Keep HLS MPEG-TS segments close to the configured 2s target
            # even when an input camera delivers a variable frame cadence.
            encoder.set_property("iframeinterval", 15)
            encoder.set_property("idrinterval", 15)
            encoder.set_property("preset-id", 1)
            output_caps = "video/x-raw(memory:NVMM),format=I420"
            LOG.info("output encoder: nvv4l2h264enc")
        except RuntimeError:
            encoder = make_element("x264enc", "output-encoder")
            encoder.set_property("bitrate", 4_000)
            encoder.set_property("speed-preset", "ultrafast")
            encoder.set_property("tune", "zerolatency")
            encoder.set_property("key-int-max", 15)
            output_caps = "video/x-raw,format=I420"
            LOG.warning("output encoder: x264enc fallback")
        output_i420_caps = make_element("capsfilter", "output-i420-caps")
        output_fps = int(input_cfg.get("output_fps", 15))
        output_i420_caps.set_property(
            "caps",
            Gst.Caps.from_string(f"{output_caps},framerate={output_fps}/1"),
        )
        output_parser = make_element("h264parse", "output-h264-parse")
        output_parser.set_property("config-interval", 1)
        sink = make_element("rtspclientsink", "rtsp-output")
        sink.set_property("location", output_cfg["rtsp_url"])
        sink.set_property("protocols", 4)
        sink.set_property("latency", 100)

        elements = [
            source,
            self.depay,
            parser,
            decoder,
            mux,
            self.person_infer,
            analysis_tee,
            analysis_queue,
            output_input_queue,
            *(
                [face_cpu_convert, face_cpu_caps]
                if self.face_engine.enabled or self.smoking_behavior_enabled or self.fire_smoke_enabled
                else []
            ),
            analysis_sink,
            convert_before_osd,
            face_rgba_caps,
            osd,
            convert_after_osd,
            output_rate,
            output_i420_caps,
            output_tee,
            output_queue,
            encoder,
            output_parser,
            sink,
        ]
        self.pipeline.add(*elements)
        source.connect("pad-added", self._on_source_pad_added)
        if not self.depay.link(parser) or not parser.link(decoder):
            raise RuntimeError("Unable to link RTSP depayloader to decoder")
        decoder_src = decoder.get_static_pad("src")
        mux_sink = mux.get_request_pad("sink_0")
        if decoder_src is None or mux_sink is None or decoder_src.link(mux_sink) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Unable to link decoder to nvstreammux")
        if not mux.link(self.person_infer):
            raise RuntimeError("Unable to link nvstreammux to person nvinfer")
        if not self.person_infer.link(analysis_tee):
            raise RuntimeError("Unable to link person inference to analysis tee")
        analysis_tee_pad = analysis_tee.get_request_pad("src_%u")
        analysis_sink_pad = analysis_queue.get_static_pad("sink")
        if (
            analysis_tee_pad is None
            or analysis_sink_pad is None
            or analysis_tee_pad.link(analysis_sink_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError("Unable to link analysis tee to analysis queue")
        output_tee_pad = analysis_tee.get_request_pad("src_%u")
        output_sink_pad = output_input_queue.get_static_pad("sink")
        if (
            output_tee_pad is None
            or output_sink_pad is None
            or output_tee_pad.link(output_sink_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError("Unable to link analysis tee to output queue")
        needs_cpu_frame = self.face_engine.enabled or self.smoking_behavior_enabled or self.fire_smoke_enabled
        if needs_cpu_frame:
            if not analysis_queue.link(face_cpu_convert):
                raise RuntimeError("Unable to link analysis source to face converter")
            if not face_cpu_convert.link(face_cpu_caps):
                raise RuntimeError("Unable to link face converter to CPU caps")
            analysis_src = face_cpu_caps
        else:
            analysis_src = analysis_queue
        if not analysis_src.link(analysis_sink):
            raise RuntimeError("Unable to terminate analysis branch")
        if not output_input_queue.link(convert_before_osd):
            raise RuntimeError("Unable to link output queue to OSD converter")
        infer_src = self.person_infer.get_static_pad("src")
        if infer_src is None:
            raise RuntimeError("Person nvinfer has no src pad")
        if not convert_before_osd.link(face_rgba_caps):
            raise RuntimeError("Unable to link CPU face frame to OSD converter")
        if not face_rgba_caps.link(osd) or not osd.link(convert_after_osd):
            raise RuntimeError("Unable to link OSD branch")
        if not convert_after_osd.link(output_rate) or not output_rate.link(output_i420_caps):
            raise RuntimeError("Unable to link output rate normalizer")
        if not output_i420_caps.link(output_tee):
            raise RuntimeError("Unable to link output tee")
        if not output_tee.link(output_queue):
            raise RuntimeError("Unable to link output branches")
        if not output_queue.link(encoder) or not encoder.link(output_parser):
            raise RuntimeError("Unable to link output encoder")
        if not output_parser.link(sink):
            raise RuntimeError("Unable to link RTSP output")
        # Attach output metadata immediately before nvdsosd.  Display metadata
        # added after nvdsosd has already rendered is invisible in the encoded
        # stream, even though the same coordinates remain available to API
        # consumers.
        osd_input_src = face_rgba_caps.get_static_pad("src")
        if osd_input_src is None:
            raise RuntimeError("OSD input has no src pad")
        osd_input_src.add_probe(Gst.PadProbeType.BUFFER, self._on_output_buffer)
        person_src = self.person_infer.get_static_pad("src")
        if person_src is None:
            raise RuntimeError("Person nvinfer has no src pad")
        # Create person objects before assigning stable application track IDs;
        # otherwise behavior and face recognition see every object as UNTRACKED.
        person_src.add_probe(Gst.PadProbeType.BUFFER, self._on_person_buffer)
        person_src.add_probe(Gst.PadProbeType.BUFFER, self._on_metadata_buffer)
        face_src = analysis_src.get_static_pad("src")
        if face_src is None:
            raise RuntimeError("Analysis probe has no src pad")
        self.frame_probe_id = face_src.add_probe(
            Gst.PadProbeType.BUFFER, self._on_behavior_buffer
        )

    def _on_source_pad_added(self, source: Gst.Element, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            return
        structure = caps.get_structure(0)
        if not structure.get_name().startswith("application/x-rtp"):
            return
        if self.depay is None:
            return
        sink_pad = self.depay.get_static_pad("sink")
        if sink_pad is not None and not sink_pad.is_linked():
            result = pad.link(sink_pad)
            if result != Gst.PadLinkReturn.OK:
                LOG.error("Unable to link RTSP source: %s", result)

    @staticmethod
    def _tensor_meta_from_list(user_list: Any, unique_id: int | None = None) -> Any | None:
        current = user_list
        while current is not None:
            try:
                user_meta = pyds.NvDsUserMeta.cast(current.data)
                if user_meta.base_meta.meta_type == pyds.NVDSINFER_TENSOR_OUTPUT_META:
                    tensor_meta = pyds.NvDsInferTensorMeta.cast(user_meta.user_meta_data)
                    tensor_unique_id = getattr(tensor_meta, "unique_id", None)
                    if unique_id is None or tensor_unique_id is None or int(tensor_unique_id) == unique_id:
                        return tensor_meta
                current = current.next
            except StopIteration:
                break
        return None

    def _get_tensor_meta(self, frame_meta: Any, batch_meta: Any, unique_id: int) -> Any | None:
        tensor_meta = self._tensor_meta_from_list(frame_meta.frame_user_meta_list, unique_id)
        if tensor_meta is not None:
            return tensor_meta
        tensor_meta = self._tensor_meta_from_list(batch_meta.batch_user_meta_list, unique_id)
        if tensor_meta is not None:
            return tensor_meta
        return None

    @staticmethod
    def _layer_array(layer: Any) -> tuple[np.ndarray, tuple[int, ...]]:
        dims = tuple(int(layer.inferDims.d[index]) for index in range(layer.inferDims.numDims))
        elements = int(layer.inferDims.numElements)
        ptr = pyds.get_ptr(layer.buffer)
        if ptr == 0:
            raise RuntimeError("nvinfer returned a null tensor pointer")
        if int(layer.dataType) == int(pyds.NvDsInferDataType.HALF):
            raw = np.ctypeslib.as_array(
                ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16)), shape=(elements,)
            ).copy()
            return raw.view(np.float16).astype(np.float32), dims
        raw = np.ctypeslib.as_array(
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_float)), shape=(elements,)
        ).copy()
        return raw, dims

    @staticmethod
    def _deduplicate_person_boxes(boxes: list[np.ndarray]) -> list[np.ndarray]:
        if len(boxes) < 2:
            return boxes
        ordered = sorted(boxes, key=lambda box: float(box[4]), reverse=True)
        kept: list[np.ndarray] = []
        for candidate in ordered:
            c_left, c_top, c_right, c_bottom = [float(value) for value in candidate[:4]]
            c_area = max(1.0, (c_right - c_left) * (c_bottom - c_top))
            duplicate = False
            for existing in kept:
                e_left, e_top, e_right, e_bottom = [float(value) for value in existing[:4]]
                e_area = max(1.0, (e_right - e_left) * (e_bottom - e_top))
                left = max(c_left, e_left)
                top = max(c_top, e_top)
                right = min(c_right, e_right)
                bottom = min(c_bottom, e_bottom)
                intersection = max(0.0, right - left) * max(0.0, bottom - top)
                union = c_area + e_area - intersection
                contained = intersection / min(c_area, e_area)
                if intersection / max(1.0, union) >= 0.25 or contained >= 0.65:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(candidate)
        return kept

    def _decode_person_boxes(self, tensor_meta: Any, frame_width: int, frame_height: int) -> list[np.ndarray]:
        model = self.config["person"]
        threshold = float(model["confidence"])
        input_width = float(model["input_width"])
        input_height = float(model["input_height"])
        for index in range(int(tensor_meta.num_output_layers)):
            layer = pyds.get_nvds_LayerInfo(tensor_meta, index)
            values, dims = self._layer_array(layer)
            if not self.person_tensor_logged:
                LOG.info(
                    "person tensor layers=%d index=%d dims=%s min=%.6f max=%.6f first=%s",
                    int(tensor_meta.num_output_layers),
                    index,
                    dims,
                    float(values.min()),
                    float(values.max()),
                    np.round(values[:10], 4).tolist(),
                )
                if index == int(tensor_meta.num_output_layers) - 1:
                    self.person_tensor_logged = True
            if len(dims) == 3 and dims[0] == 1:
                dims = dims[1:]
            if len(dims) != 2:
                continue
            if dims[0] < dims[1]:
                matrix = values.reshape(dims).T
            elif dims[1] < dims[0]:
                matrix = values.reshape((dims[1], dims[0])).T
            else:
                matrix = values.reshape(dims)
            if matrix.shape[1] < 5:
                continue
            if not self.person_score_logged:
                person_scores = matrix[:, 4]
                LOG.info(
                    "person scores max=%.6f count_ge_%.2f=%d",
                    float(person_scores.max()),
                    threshold,
                    int((person_scores >= threshold).sum()),
                )
                self.person_score_logged = True
            boxes: list[np.ndarray] = []
            scale_x = frame_width / input_width
            scale_y = frame_height / input_height
            for row in matrix:
                score = float(row[4])
                if score < threshold:
                    continue
                center_x, center_y, width, height = [float(value) for value in row[:4]]
                left = max(0.0, (center_x - width / 2.0) * scale_x)
                top = max(0.0, (center_y - height / 2.0) * scale_y)
                right = min(float(frame_width), (center_x + width / 2.0) * scale_x)
                bottom = min(float(frame_height), (center_y + height / 2.0) * scale_y)
                if right > left and bottom > top:
                    boxes.append(np.array([left, top, right, bottom, score], dtype=np.float32))
            return nms(boxes, float(model["iou"]))
        return []

    @staticmethod
    def _frame_objects(frame_meta: Any) -> list[Any]:
        objects: list[Any] = []
        current = frame_meta.obj_meta_list
        while current is not None:
            try:
                objects.append(pyds.NvDsObjectMeta.cast(current.data))
                current = current.next
            except StopIteration:
                break
        return objects

    def _decode_bgrx_buffer(self, buffer: Any, width: int, height: int) -> np.ndarray | None:
        """Map the CPU BGRx analysis surface using the stride-aware decoder."""
        return self.face_engine.decode_bgrx_frame(buffer, width, height)

    def _queue_analysis_sample(
        self,
        frame_num: int,
        frame: np.ndarray | None,
        persons: list[tuple[int, float, float, float, float]],
    ) -> None:
        if frame is None or not self.analysis_enabled:
            return
        sample = (frame.copy(), persons, frame_num, time.time())
        try:
            self._analysis_queue.put_nowait(sample)
        except queue.Full:
            try:
                self._analysis_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._analysis_queue.put_nowait(sample)
            except queue.Full:
                pass
        self._analysis_enqueued_count += 1
        self._analysis_last_enqueued_frame = int(frame_num)

    def _analysis_loop(self) -> None:
        while not self._analysis_stop.is_set():
            try:
                sample = self._analysis_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if sample is None:
                break
            frame, persons, frame_num, timestamp = sample
            self._analysis_processed_count += 1
            self._analysis_last_processed_frame = int(frame_num)
            try:
                face_tracks: dict[int, dict[str, Any]] = {}
                if self.face_engine.enabled:
                    face_tracks = self.face_engine.process_frame(frame, persons, int(frame_num))
                detection_results: list[Any] = []
                fire_smoke_detections: list[Any] = []
                if self.smoking_behavior_enabled and self.smoking_behavior_engine is not None:
                    detection_results = self.smoking_behavior_engine.process(
                        frame, persons, int(frame_num)
                    )
                if self.fire_smoke_enabled and self.fire_smoke_engine is not None:
                    fire_smoke_detections = self.fire_smoke_engine.process(frame)
                detections = [
                    SafetyDetection(
                        track_id=detection.track_id,
                        score=float(detection.score),
                        bbox=detection.person_bbox,
                        model_roi_bbox=detection.model_roi_bbox,
                    )
                    for detection in detection_results
                ]
                transitions: list[Any] = []
                transition = (
                    self.event_store.observe(
                        frame_num=int(frame_num),
                        timestamp=timestamp,
                        detections=detections,
                        frame=frame,
                    )
                    if self.smoking_behavior_enabled
                    else None
                )
                if transition is not None:
                    transitions.append(transition)
                if self.fire_smoke_enabled:
                    if (
                        self.fire_smoke_engine is not None
                        and self.fire_smoke_engine.last_inference_ran
                    ):
                        transitions.extend(
                            self.fire_smoke_events.observe(
                                frame_num=int(frame_num),
                                timestamp=timestamp,
                                detections=list(
                                    self.fire_smoke_engine.last_fresh_detections
                                ),
                                frame=frame,
                            )
                        )
                self._notify_transitions(transitions)
                with self._analysis_lock:
                    self._face_tracks = dict(face_tracks)
                    self._analysis_detections = list(detection_results)
                    self._analysis_fire_smoke = list(fire_smoke_detections)
                    self._analysis_transitions = transitions
                    self._analysis_frame_num = int(frame_num)
                    self._analysis_updated_at = time.time()
                    self._analysis_last_transition = (
                        transitions[-1].operation if transitions else None
                    )
                    self.last_bbox_count = len(detection_results)
                    self.last_fire_smoke_count = len(fire_smoke_detections)
            except Exception as exc:
                message = str(exc)
                if message != self._analysis_error:
                    LOG.exception("background analysis failed: %s", message)
                    self._analysis_error = message

    def _cached_analysis(self) -> tuple[list[Any], list[Any], list[Any], str | None]:
        with self._analysis_lock:
            return (
                list(self._analysis_detections),
                list(self._analysis_fire_smoke),
                list(self._analysis_transitions),
                self._analysis_last_transition,
            )

    def _publish_live_metadata(self, payload: dict[str, Any]) -> None:
        """Publish latest overlay metadata without putting disk I/O on every frame."""
        now = time.monotonic()
        if now - self._metadata_write_at < 0.10:
            return
        self._metadata_write_at = now
        try:
            self.status_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.metadata_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.metadata_path)
        except OSError as exc:
            LOG.debug("live metadata write failed: %s", exc)

    def _render_output_annotations(
        self,
        batch_meta: Any,
        frame_meta: Any,
        detection_results: list[Any],
        fire_smoke_detections: list[Any],
    ) -> None:
        """Render cached inference results on the independent live-output branch."""
        if self.smoking_behavior_enabled:
            self._attach_objects(frame_meta, detection_results)
        if self.fire_smoke_enabled:
            self._attach_fire_smoke_objects(batch_meta, frame_meta, fire_smoke_detections)
        labels: list[tuple[str, int, int]] = []
        for obj_meta in self._frame_objects(frame_meta):
            object_label = str(obj_meta.obj_label)
            if object_label not in {self.config["person"]["label"], "fire", "smoke"}:
                continue
            if object_label == self.config["person"]["label"]:
                name, score = self.face_engine.current_label(
                    int(obj_meta.object_id), int(frame_meta.frame_num)
                )
                track_id = int(obj_meta.object_id)
                smoking = self._smoking_by_track.get(track_id)
                if smoking is not None:
                    # Keep the current-frame tracker rectangle. The behavior
                    # result is asynchronous and is used only for the label;
                    # reusing its older ROI is what causes visible drift.
                    label = f"person #{track_id} | SMOKING {float(smoking.score) * 100:.0f}%"
                    obj_meta.rect_params.border_width = 4
                    obj_meta.rect_params.border_color.set(1.0, 0.0, 0.0, 1.0)
                else:
                    obj_meta.rect_params.border_width = 3
                    obj_meta.rect_params.border_color.set(0.0, 0.65, 1.0, 1.0)
                    obj_meta.rect_params.has_bg_color = 0
                    label = f"person #{track_id} | {name} {score:.2f}"
            else:
                display_label = "SMOKE AREA" if object_label == "smoke" else "FIRE"
                label = f"{display_label} {float(obj_meta.confidence) * 100:.0f}%"
            labels.append(
                (label, int(obj_meta.rect_params.left), max(0, int(obj_meta.rect_params.top) - 26))
            )
            obj_meta.text_params.display_text = ""
            obj_meta.text_params.set_bg_clr = 0
        if labels:
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_labels = len(labels)
            for index, (label, left, top) in enumerate(labels):
                text_params = display_meta.text_params[index]
                text_params.display_text = label
                text_params.x_offset = left
                text_params.y_offset = top
                text_params.font_params.font_name = "Sans"
                text_params.font_params.font_size = 16
                text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
                text_params.set_bg_clr = 0
            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        self._add_live_timestamp(batch_meta, frame_meta)

    def _on_output_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """Render backend-owned labels without applying an old ROI to a new frame."""
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        with self._analysis_lock:
            analysis_frame_num = self._analysis_frame_num
            current_frame_num = 0
            if batch_meta.frame_meta_list is not None:
                try:
                    current_frame = pyds.NvDsFrameMeta.cast(batch_meta.frame_meta_list.data)
                    current_frame_num = int(current_frame.frame_num)
                except Exception:
                    current_frame_num = 0
            analysis_age = (
                abs(current_frame_num - analysis_frame_num)
                if analysis_frame_num is not None
                else self._analysis_max_age_frames + 1
            )
            if analysis_age <= self._analysis_max_age_frames:
                detection_results = list(self._analysis_detections)
                fire_smoke_detections = list(self._analysis_fire_smoke)
            else:
                detection_results = []
                fire_smoke_detections = []
        frame_list = batch_meta.frame_meta_list
        while frame_list is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(frame_list.data)
                self._render_output_annotations(
                    batch_meta, frame_meta, detection_results, fire_smoke_detections
                )
                frame_list = frame_list.next
            except StopIteration:
                break
            except Exception as exc:
                LOG.debug("live output annotation failed: %s", exc)
                break
        return Gst.PadProbeReturn.OK

    def _stop_analysis_worker(self) -> None:
        if self._analysis_thread is None:
            return
        self._analysis_stop.set()
        try:
            self._analysis_queue.put_nowait(None)
        except queue.Full:
            try:
                self._analysis_queue.get_nowait()
                self._analysis_queue.put_nowait(None)
            except queue.Empty:
                pass
        self._analysis_thread.join(timeout=5)
        self._analysis_thread = None

    def _write_runtime_status(self) -> bool:
        payload = {
            "camera": self.config["input"].get("camera", "unknown"),
            "run_id": self.run_id,
            "worker_epoch": self.evidence.worker_epoch,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": time.time(),
            "last_frame_at": self.last_frame_at,
            "frame_count": self.frame_count,
            "analysis_queue_depth": self._analysis_queue.qsize(),
            "analysis_error": self._analysis_error,
            "analysis_flow": {
                "probe_count": self._analysis_probe_count,
                "due_count": self._analysis_due_count,
                "enqueued_count": self._analysis_enqueued_count,
                "processed_count": self._analysis_processed_count,
                "last_enqueued_frame": self._analysis_last_enqueued_frame,
                "last_processed_frame": self._analysis_last_processed_frame,
            },
            "analysis_decode": getattr(self.face_engine, "_last_decode_info", None),
            "analysis_debug": {
                "smoking_person_count": getattr(self.smoking_behavior_engine, "last_person_count", 0),
                "smoking_scores": getattr(self.smoking_behavior_engine, "last_scores", {}),
                "smoking_score_histories": getattr(
                    self.smoking_behavior_engine, "last_histories", {}
                ),
                "smoking_roi_bboxes": getattr(
                    self.smoking_behavior_engine, "last_roi_bboxes", {}
                ),
                "smoking_confirmed_tracks": getattr(
                    self.smoking_behavior_engine, "last_confirmed_tracks", []
                ),
                "fire_smoke_raw_scores": getattr(self.fire_smoke_engine, "last_raw_scores", {}),
                "person_detector_count": self.last_person_count,
                "metadata_person_count": self._last_metadata_person_count,
                "behavior_person_count": self._last_behavior_person_count,
                "latest_person_count": len(self._latest_person_rois),
                "latest_person_age_seconds": (
                    round(time.monotonic() - self._latest_person_updated_at, 3)
                    if self._latest_person_updated_at is not None
                    else None
                ),
            },
            "notifications": self.notifications.status(),
        }
        try:
            self.status_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.status_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.status_path)
        except OSError as exc:
            LOG.warning("runtime status write failed: %s", exc)
        return True

    def _person_rois(self, frame_meta: Any) -> list[tuple[int, float, float, float, float]]:
        persons: list[tuple[int, float, float, float, float]] = []
        for obj_meta in self._frame_objects(frame_meta):
            if str(obj_meta.obj_label) != self.config["person"]["label"]:
                continue
            track_id = int(obj_meta.object_id)
            if track_id in {0, 18446744073709551615}:
                continue
            left = float(obj_meta.rect_params.left)
            top = float(obj_meta.rect_params.top)
            right = left + float(obj_meta.rect_params.width)
            bottom = top + float(obj_meta.rect_params.height)
            persons.append((track_id, left, top, right, bottom))
        return persons

    def _on_person_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        frame_list = batch_meta.frame_meta_list
        while frame_list is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(frame_list.data)
                self.last_frame_at = time.time()
                tensor_meta = self._get_tensor_meta(frame_meta, batch_meta, 2)
                person_count = 0
                if tensor_meta is not None:
                    boxes = self._decode_person_boxes(
                        tensor_meta,
                        int(self.config["input"]["width"]),
                        int(self.config["input"]["height"]),
                    )
                    boxes = self._deduplicate_person_boxes(boxes)
                    person_count = len(boxes)
                    for box in boxes:
                        obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
                        obj_meta.class_id = 0
                        obj_meta.unique_component_id = 2
                        # This object is created by the custom tensor decoder. Mark it
                        # as untracked so nvtracker assigns the authoritative object_id.
                        obj_meta.object_id = 18446744073709551615
                        obj_meta.confidence = float(box[4])
                        obj_meta.obj_label = self.config["person"]["label"]
                        obj_meta.rect_params.left = float(box[0])
                        obj_meta.rect_params.top = float(box[1])
                        obj_meta.rect_params.width = float(box[2] - box[0])
                        obj_meta.rect_params.height = float(box[3] - box[1])
                        detector_box = obj_meta.detector_bbox_info.org_bbox_coords
                        detector_box.left = float(box[0])
                        detector_box.top = float(box[1])
                        detector_box.width = float(box[2] - box[0])
                        detector_box.height = float(box[3] - box[1])
                        # Keep detector metadata for tracking, but render only the
                        # canonical annotation in the behavior probe below.
                        obj_meta.rect_params.border_width = 0
                        obj_meta.rect_params.has_bg_color = 0
                        obj_meta.text_params.display_text = ""
                        obj_meta.text_params.set_bg_clr = 0
                        pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)
                self.last_person_count = person_count
                self.person_frame_count += 1
                if self.person_frame_count % 100 == 0:
                    LOG.info("person_frames=%d person_count=%d", self.person_frame_count, person_count)
                frame_list = frame_list.next
            except StopIteration:
                break
        return Gst.PadProbeReturn.OK

    def _assign_person_track_ids(self, frame_meta: Any) -> None:
        persons = [
            obj for obj in self._frame_objects(frame_meta)
            if str(obj.obj_label) == self.config["person"]["label"]
        ]
        detections: list[tuple[Any, np.ndarray, tuple[float, float]]] = []
        for obj in persons:
            rect = obj.rect_params
            box = np.array(
                [
                    float(rect.left),
                    float(rect.top),
                    float(rect.left + rect.width),
                    float(rect.top + rect.height),
                ],
                dtype=np.float32,
            )
            detections.append((obj, box, ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)))

        frame_number = int(frame_meta.frame_num)
        frame_width = float(self.config["input"].get("width", 1920))
        frame_height = float(self.config["input"].get("height", 1080))
        pairs: list[tuple[float, float, int, int]] = []
        for track_id, track in self._person_tracks.items():
            old_box = track["box"]
            velocity = track.get("velocity", np.zeros(4, dtype=np.float32))
            gap = max(1, frame_number - int(track.get("last_frame", frame_number - 1)))
            estimate = old_box + velocity * min(gap, 3)
            for index, (_, box, center) in enumerate(detections):
                overlap = iou(old_box, box)
                if opposite_frame_edge_transition(
                    old_box, box, frame_width, frame_height
                ):
                    continue
                distance = frigate_track_distance(box, estimate)
                if track["disappeared"] > self._person_reacquire_max_disappeared:
                    if overlap < 0.15 or distance > 0.9:
                        continue
                elif distance > self._person_distance_threshold:
                    continue
                pairs.append((distance, -overlap, track_id, index))
        pairs.sort()

        matched_tracks: set[int] = set()
        matched_detections: set[int] = set()
        for _, _, track_id, index in pairs:
            if track_id in matched_tracks or index in matched_detections:
                continue
            matched_tracks.add(track_id)
            matched_detections.add(index)
            _, box, center = detections[index]
            track = self._person_tracks[track_id]
            previous_box = track["box"]
            smoothed_box = (
                previous_box * (1.0 - self._person_bbox_smoothing_alpha)
                + box * self._person_bbox_smoothing_alpha
            )
            delta = smoothed_box - previous_box
            previous_velocity = track.get(
                "velocity", np.zeros(4, dtype=np.float32)
            )
            track.update(
                box=smoothed_box,
                center=((smoothed_box[0] + smoothed_box[2]) / 2.0, smoothed_box[3]),
                velocity=previous_velocity * 0.7 + delta * 0.3,
                disappeared=0,
                last_frame=frame_number,
                frames_seen=int(track.get("frames_seen", 0)) + 1,
            )
            detections[index][0].rect_params.left = float(smoothed_box[0])
            detections[index][0].rect_params.top = float(smoothed_box[1])
            detections[index][0].rect_params.width = float(smoothed_box[2] - smoothed_box[0])
            detections[index][0].rect_params.height = float(smoothed_box[3] - smoothed_box[1])
            detections[index][0].object_id = track_id

        for track_id in list(self._person_tracks):
            if track_id in matched_tracks:
                continue
            track = self._person_tracks[track_id]
            track["disappeared"] += 1
            if track["disappeared"] > self._person_max_disappeared:
                del self._person_tracks[track_id]

        for index, (obj, box, center) in enumerate(detections):
            if index in matched_detections:
                continue
            track_id = self._next_person_track_id
            self._next_person_track_id += 1
            self._person_tracks[track_id] = {
                "box": box,
                "center": center,
                "velocity": np.zeros(4, dtype=np.float32),
                "disappeared": 0,
                "last_frame": frame_number,
                "frames_seen": 1,
            }
            obj.object_id = track_id

    def _recognition_tracks(self, frame_meta: Any) -> list[dict[str, Any]]:
        frame_num = int(frame_meta.frame_num)
        tracks: list[dict[str, Any]] = []
        current: set[TrackKey] = set()
        for obj_meta in self._frame_objects(frame_meta):
            label = str(obj_meta.obj_label)
            if label not in {"person", "car"}:
                continue
            track_id = str(getattr(obj_meta, "object_id", "0"))
            if track_id in {"0", "18446744073709551615"}:
                continue
            rect = obj_meta.rect_params
            key = TrackKey(
                str(self.config["input"].get("camera", "safety_camera")),
                self.recognition.stream_epoch,
                track_id,
            )
            current.add(key)
            self.recognition.touch(key, time.time())
            self.recognition_last_frame[key] = frame_num
            tracks.append(
                {
                    "track_id": track_id,
                    "label": label,
                    "left": round(float(rect.left), 2),
                    "top": round(float(rect.top), 2),
                    "right": round(float(rect.left + rect.width), 2),
                    "bottom": round(float(rect.top + rect.height), 2),
                }
            )
        stale_after = max(2, int(self.config["input"].get("fps", 5)) * 2)
        for key, last_frame in tuple(self.recognition_last_frame.items()):
            if key not in current and frame_num - last_frame >= stale_after:
                self.recognition.end_track(key, "not_seen")
                self.recognition_last_frame.pop(key, None)
        return tracks

    def _attach_objects(self, frame_meta: Any, detections: list[Any]) -> None:
        """Annotate the existing person object; never render ROI as a new object."""
        self._smoking_by_track = {int(item.track_id): item for item in detections}
        smoking_ids = set(self._smoking_by_track)
        for obj_meta in self._frame_objects(frame_meta):
            if str(obj_meta.obj_label) != self.config["person"]["label"]:
                continue
            track_id = int(obj_meta.object_id)
            if track_id not in smoking_ids:
                continue
            detection = self._smoking_by_track[track_id]
            obj_meta.rect_params.border_width = 4
            obj_meta.rect_params.border_color.set(0.0, 0.0, 1.0, 1.0)
            obj_meta.rect_params.has_bg_color = 0
            obj_meta.text_params.display_text = (
                f"person #{track_id} | SMOKING {float(detection.score):.2f}"
            )
            obj_meta.text_params.font_params.font_name = "Sans"
            obj_meta.text_params.font_params.font_size = 16
            obj_meta.text_params.set_bg_clr = 1
            obj_meta.text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.75)

    def _attach_fire_smoke_objects(
        self, batch_meta: Any, frame_meta: Any, detections: list[Any]
    ) -> None:
        colors = {
            "fire": (1.0, 0.35, 0.0, 1.0),
            # Bright cyan stays visible over both the dark room and the white
            # smoke plume; gray was too low-contrast on the live stream.
            "smoke": (0.0, 1.0, 1.0, 1.0),
        }
        for detection in detections:
            left, top, right, bottom = detection.bbox
            obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
            obj_meta.class_id = 0 if detection.label == "fire" else 1
            obj_meta.confidence = float(detection.score)
            obj_meta.obj_label = detection.label
            obj_meta.rect_params.left = float(left)
            obj_meta.rect_params.top = float(top)
            obj_meta.rect_params.width = float(right - left)
            obj_meta.rect_params.height = float(bottom - top)
            obj_meta.rect_params.border_width = 3
            obj_meta.rect_params.border_color.set(*colors.get(detection.label, colors["smoke"]))
            obj_meta.rect_params.has_bg_color = 0
            display_label = "SMOKE AREA" if detection.label == "smoke" else "FIRE"
            obj_meta.text_params.display_text = f"{display_label} {detection.score * 100:.0f}%"
            obj_meta.text_params.font_params.font_name = "Sans"
            obj_meta.text_params.font_params.font_size = 14
            obj_meta.text_params.set_bg_clr = 1
            obj_meta.text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.65)
            pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)

    def _publish_metadata_frame(self, frame_meta: Any) -> None:
        """Publish lightweight overlay state from the non-blocking DeepStream probe."""
        tracks = self._recognition_tracks(frame_meta)
        detection_results, fire_smoke_detections, transitions, _ = self._cached_analysis()
        with self._analysis_lock:
            face_tracks = list(self._face_tracks.values())
        face_by_track = {
            int(item.get("track_id", -1)): item for item in face_tracks
        }
        overlays: list[dict[str, Any]] = []
        for item in tracks:
            face_info = face_by_track.get(int(item["track_id"]))
            name = str(face_info.get("name", "unknown")) if face_info else "unknown"
            score = float(face_info.get("score", 0.0)) if face_info else 0.0
            overlays.append(
                {
                    "kind": "person",
                    "track_id": item["track_id"],
                    "left": item["left"],
                    "top": item["top"],
                    "right": item["right"],
                    "bottom": item["bottom"],
                    "label": f"person #{item['track_id']} | {name} {score:.2f}",
                    "score": score,
                }
            )
        for detection in detection_results:
            overlays.append(
                {
                    "kind": "smoking",
                    "track_id": detection.track_id,
                    "left": detection.person_bbox[0],
                    "top": detection.person_bbox[1],
                    "right": detection.person_bbox[2],
                    "bottom": detection.person_bbox[3],
                    "label": f"SMOKING {float(detection.score) * 100:.0f}%",
                    "score": float(detection.score),
                }
            )
        for detection in fire_smoke_detections:
            overlays.append(
                {
                    "kind": "fire" if detection.label == "fire" else "smoke",
                    "left": detection.bbox[0],
                    "top": detection.bbox[1],
                    "right": detection.bbox[2],
                    "bottom": detection.bbox[3],
                    "label": (
                        f"{'FIRE' if detection.label == 'fire' else 'SMOKE AREA'} "
                        f"{float(detection.score) * 100:.0f}%"
                    ),
                    "score": float(detection.score),
                }
            )
        payload = {
            "camera": self.config["input"].get("camera", "safety_camera"),
            "run_id": self.run_id,
            "width": int(self.config["input"].get("width", 1920)),
            "height": int(self.config["input"].get("height", 1080)),
            "frame_num": int(frame_meta.frame_num),
            "timestamp": time.time(),
            "bbox_count": len(detection_results),
            "fire_smoke_count": len(fire_smoke_detections),
            "event_id": self.event_store.active_event_id,
            "event_state": self.event_store.state.value,
            "fire_smoke_events": [
                {
                    "operation": item.operation,
                    "event_id": item.event_id,
                    "label": getattr(item, "label", "smoking"),
                }
                for item in transitions
            ],
            "fire_smoke_active_event_ids": self.fire_smoke_events.active_event_ids,
            "recognition_enabled": self.recognition.enabled,
            "tracks": tracks,
            "face_tracks": face_tracks,
            "overlays": overlays,
            "boxes": [
                {
                    "label": "person",
                    "behavior": "smoking",
                    "track_id": detection.track_id,
                    "confidence": round(float(detection.score), 5),
                    "left": round(float(detection.person_bbox[0]), 2),
                    "top": round(float(detection.person_bbox[1]), 2),
                    "right": round(float(detection.person_bbox[2]), 2),
                    "bottom": round(float(detection.person_bbox[3]), 2),
                }
                for detection in detection_results
            ],
            "fire_smoke": [
                {
                    "label": detection.label,
                    "confidence": round(float(detection.score), 5),
                    "left": round(float(detection.bbox[0]), 2),
                    "top": round(float(detection.bbox[1]), 2),
                    "right": round(float(detection.bbox[2]), 2),
                    "bottom": round(float(detection.bbox[3]), 2),
                }
                for detection in fire_smoke_detections
            ],
        }
        self._publish_live_metadata(payload)
        self.frame_count += 1
        if self.frame_count % 100 == 0:
            LOG.info(
                "frames=%d smoking_bbox_count=%d fire_smoke_count=%d",
                self.frame_count,
                len(detection_results),
                len(fire_smoke_detections),
            )

    def _on_metadata_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        frame_list = batch_meta.frame_meta_list
        while frame_list is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(frame_list.data)
                self._assign_person_track_ids(frame_meta)
                person_rois = self._person_rois(frame_meta)
                self._last_metadata_person_count = len(person_rois)
                with self._analysis_lock:
                    # The CPU analysis branch can lag the inference branch by
                    # more than a couple of frame numbers after a leaky tee.
                    # Keep the latest non-empty tracker ROI with a wall-clock
                    # age so recognition/behavior never loses all objects just
                    # because NvDs metadata was not copied through conversion.
                    if person_rois:
                        self._latest_person_frame_num = int(frame_meta.frame_num)
                        self._latest_person_rois = person_rois
                        self._latest_person_updated_at = time.monotonic()
                self._publish_metadata_frame(frame_meta)
                frame_list = frame_list.next
            except StopIteration:
                break
        return Gst.PadProbeReturn.OK

    def _add_live_timestamp(self, batch_meta: Any, frame_meta: Any) -> None:
        """Stamp output frames with wall-clock time for live/event comparison."""
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        text_params = display_meta.text_params[0]
        text_params.display_text = (
            f"{self.config['input'].get('camera', 'camera')} | "
            f"LIVE {dt.datetime.now().astimezone().strftime('%H:%M:%S.%f')[:-3]}"
        )
        text_params.x_offset = 24
        text_params.y_offset = 50
        text_params.font_params.font_name = "Sans"
        text_params.font_params.font_size = 32
        text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
        text_params.set_bg_clr = 1
        text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.70)
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

    def _on_behavior_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        frame_list = batch_meta.frame_meta_list
        while frame_list is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(frame_list.data)
                self._analysis_probe_count += 1
                frame: np.ndarray | None = None
                boxes: list[np.ndarray] = []
                detections: list[SafetyDetection] = []
                detection_results: list[Any] = []
                fire_smoke_detections: list[Any] = []
                if self.analysis_enabled:
                    frame_num = int(frame_meta.frame_num)
                    now = time.monotonic()
                    if now >= self._next_analysis_at:
                        self._analysis_due_count += 1
                        self._next_analysis_at = now + self._analysis_interval_seconds
                        frame_width = int(
                            getattr(frame_meta, "source_frame_width", 0)
                            or self.config["input"]["width"]
                        )
                        frame_height = int(
                            getattr(frame_meta, "source_frame_height", 0)
                            or self.config["input"]["height"]
                        )
                        frame = self._decode_bgrx_buffer(
                            buffer,
                            frame_width,
                            frame_height,
                        )
                        persons = self._person_rois(frame_meta)
                        if not persons:
                            with self._analysis_lock:
                                cache_age = (
                                    time.monotonic() - self._latest_person_updated_at
                                    if self._latest_person_updated_at is not None
                                    else None
                                )
                                if cache_age is not None and cache_age <= 1.0:
                                    persons = list(self._latest_person_rois)
                        self._last_behavior_person_count = len(persons)
                        self._queue_analysis_sample(frame_num, frame, persons)
                    (
                        detection_results,
                        fire_smoke_detections,
                        cached_transitions,
                        cached_transition,
                    ) = self._cached_analysis()
                    boxes = [
                        np.array(
                            [*detection.person_bbox, detection.score],
                            dtype=np.float32,
                        )
                        for detection in detection_results
                    ]
                    detections = [
                        SafetyDetection(
                            track_id=detection.track_id,
                            score=float(detection.score),
                            bbox=detection.person_bbox,
                            model_roi_bbox=detection.model_roi_bbox,
                        )
                        for detection in detection_results
                    ]
                else:
                    cached_transitions = []
                    cached_transition = None
                tracks = self._recognition_tracks(frame_meta)
                self.last_bbox_count = len(boxes)
                self.last_fire_smoke_count = len(fire_smoke_detections)
                if self.analysis_enabled:
                    fire_smoke_transitions = cached_transitions
                    self.last_event_transition = cached_transition
                else:
                    transition = (
                        self.event_store.observe(
                            frame_num=int(frame_meta.frame_num),
                            timestamp=time.time(),
                            detections=detections,
                            frame=frame,
                        )
                        if self.smoking_behavior_enabled
                        else None
                    )
                    self.last_event_transition = transition.operation if transition else None
                    fire_smoke_transitions = (
                        self.fire_smoke_events.observe(
                            frame_num=int(frame_meta.frame_num),
                            timestamp=time.time(),
                            detections=fire_smoke_detections,
                            frame=frame,
                        )
                        if self.fire_smoke_enabled
                        else []
                    )
                    if transition is not None:
                        self._notify_transitions([transition])
                    self._notify_transitions(fire_smoke_transitions)
                with self._analysis_lock:
                    face_tracks = list(self._face_tracks.values())
                overlays: list[dict[str, Any]] = []
                for item in tracks:
                    face_info = next(
                        (
                            candidate
                            for candidate in face_tracks
                            if int(candidate.get("track_id", -1)) == int(item["track_id"])
                        ),
                        None,
                    )
                    name = str(face_info.get("name", "unknown")) if face_info else "unknown"
                    score = float(face_info.get("score", 0.0)) if face_info else 0.0
                    overlays.append(
                        {
                            "kind": "person",
                            "track_id": item["track_id"],
                            "left": item["left"],
                            "top": item["top"],
                            "right": item["right"],
                            "bottom": item["bottom"],
                            "label": f"person #{item['track_id']} | {name} {score:.2f}",
                            "score": score,
                        }
                    )
                for detection in detection_results:
                    overlays.append(
                        {
                            "kind": "smoking",
                            "track_id": detection.track_id,
                            "left": detection.person_bbox[0],
                            "top": detection.person_bbox[1],
                            "right": detection.person_bbox[2],
                            "bottom": detection.person_bbox[3],
                            "label": f"SMOKING {float(detection.score) * 100:.0f}%",
                            "score": float(detection.score),
                        }
                    )
                for detection in fire_smoke_detections:
                    overlays.append(
                        {
                            "kind": "fire" if detection.label == "fire" else "smoke",
                            "left": detection.bbox[0],
                            "top": detection.bbox[1],
                            "right": detection.bbox[2],
                            "bottom": detection.bbox[3],
                            "label": f"{'FIRE' if detection.label == 'fire' else 'SMOKE AREA'} {float(detection.score) * 100:.0f}%",
                            "score": float(detection.score),
                        }
                    )
                payload = {
                    "camera": self.config["input"].get("camera", "safety_camera"),
                    "run_id": self.run_id,
                    "width": int(self.config["input"].get("width", 1920)),
                    "height": int(self.config["input"].get("height", 1080)),
                    "frame_num": int(frame_meta.frame_num),
                    "timestamp": time.time(),
                    "bbox_count": len(boxes),
                    "fire_smoke_count": len(fire_smoke_detections),
                    "event_id": self.event_store.active_event_id,
                    "event_state": self.event_store.state.value,
                    "fire_smoke_events": [
                        {
                            "operation": item.operation,
                            "event_id": item.event_id,
                            "label": getattr(item, "label", "smoking"),
                        }
                        for item in fire_smoke_transitions
                    ],
                    "fire_smoke_active_event_ids": self.fire_smoke_events.active_event_ids,
                    "recognition_enabled": self.recognition.enabled,
                    "tracks": tracks,
                    "face_tracks": face_tracks,
                    "overlays": overlays,
                    "boxes": [
                        {
                            "label": "person",
                            "behavior": "smoking",
                            "track_id": detection.track_id,
                            "confidence": round(float(detection.score), 5),
                            "left": round(float(detection.person_bbox[0]), 2),
                            "top": round(float(detection.person_bbox[1]), 2),
                            "right": round(float(detection.person_bbox[2]), 2),
                            "bottom": round(float(detection.person_bbox[3]), 2),
                            "model_roi": {
                                "left": round(float(detection.model_roi_bbox[0]), 2),
                                "top": round(float(detection.model_roi_bbox[1]), 2),
                                "right": round(float(detection.model_roi_bbox[2]), 2),
                                "bottom": round(float(detection.model_roi_bbox[3]), 2),
                            },
                        }
                        for detection in detection_results
                    ],
                    "fire_smoke": [
                        {
                            "label": detection.label,
                            "confidence": round(float(detection.score), 5),
                            "left": round(float(detection.bbox[0]), 2),
                            "top": round(float(detection.bbox[1]), 2),
                            "right": round(float(detection.bbox[2]), 2),
                            "bottom": round(float(detection.bbox[3]), 2),
                        }
                        for detection in fire_smoke_detections
                    ],
                }
                self.socket.send_json(payload, flags=zmq.NOBLOCK)
                frame_list = frame_list.next
            except StopIteration:
                break
            except Exception as exc:  # keep video flowing if one malformed ROI arrives
                message = str(exc)
                if message != self.last_behavior_error:
                    LOG.exception("Analysis branch failed: %s", message)
                    self.last_behavior_error = message
                break
        return Gst.PadProbeReturn.OK

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            LOG.error("GStreamer error: %s; debug=%s", error, debug)
            self.loop.quit()
        elif message.type == Gst.MessageType.EOS:
            LOG.info("GStreamer EOS")
            self.loop.quit()
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            LOG.warning("GStreamer warning: %s; debug=%s", warning, debug)

    def _start_mock_publisher(self) -> None:
        input_cfg = self.config["input"]
        if str(input_cfg.get("mode", "rtsp")) != "mock":
            return
        mock_video = Path(str(input_cfg["mock_video"]))
        if not mock_video.is_file():
            raise FileNotFoundError(f"mock_video does not exist: {mock_video}")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
        ]
        if bool(input_cfg.get("mock_loop", True)):
            command.extend(["-stream_loop", "-1"])
        command.extend(
            [
                "-i",
                str(mock_video),
                "-r",
                "15",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "5",
                "-keyint_min",
                "5",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "rtsp",
                str(input_cfg["rtsp_url"]),
            ]
        )
        self.mock_publisher = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # MediaMTX registers the publisher asynchronously. Let rtspsrc perform
        # the actual connection after a short registration window, but fail if
        # ffmpeg itself has already exited.
        time.sleep(4.0)
        if self.mock_publisher.poll() is not None:
            raise RuntimeError("mock publisher exited before pipeline startup")
        LOG.info("mock input started: %s", mock_video)

    def _check_mock_publisher(self) -> bool:
        input_cfg = self.config["input"]
        if self.mock_publisher is None or bool(input_cfg.get("mock_loop", True)):
            return GLib.SOURCE_REMOVE
        if self.mock_publisher.poll() is not None:
            LOG.info("mock input reached EOF; stopping pipeline")
            self.loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _stop_mock_publisher(self) -> None:
        if self.mock_publisher is None:
            return
        if self.mock_publisher.poll() is None:
            self.mock_publisher.terminate()
            try:
                self.mock_publisher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.mock_publisher.kill()
                self.mock_publisher.wait(timeout=5)
        self.mock_publisher = None

    def run(self, duration: int | None = None) -> None:
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        try:
            self._start_mock_publisher()
            self.pipeline.set_state(Gst.State.PLAYING)
            self._write_runtime_status()
            GLib.timeout_add_seconds(2, self._write_runtime_status)
            if str(self.config["input"].get("mode", "rtsp")) == "mock" and not bool(
                self.config["input"].get("mock_loop", True)
            ):
                GLib.timeout_add_seconds(1, self._check_mock_publisher)
            if duration:
                GLib.timeout_add_seconds(duration, self._stop_after_duration)
            LOG.info("pipeline started; output=%s", self.config["output"]["rtsp_url"])
            self.loop.run()
        finally:
            self._stop_mock_publisher()
            self._stop_analysis_worker()
            try:
                self.face_engine.close()
            except Exception:
                LOG.exception("face lifecycle close failed; continuing shutdown")
            self.pipeline.set_state(Gst.State.NULL)
            closing_event_ids = set(self.event_store.active_event_ids)
            closing_event_ids.update(self.fire_smoke_events.active_event_ids)
            try:
                self.event_store.close()
            except Exception:
                LOG.exception("smoking event close failed; continuing shutdown")
            try:
                self.fire_smoke_events.close()
            except Exception:
                LOG.exception("fire/smoke event close failed; continuing shutdown")
            for event_id in closing_event_ids:
                self._notify_event(event_id, "END")
            try:
                self.notifications.close()
            except Exception:
                LOG.exception("notification service close failed; continuing shutdown")
            try:
                self.evidence.close()
            except Exception:
                LOG.exception("evidence close failed; continuing shutdown")
            self.socket.close(0)
            try:
                self.status_path.unlink(missing_ok=True)
                self.metadata_path.unlink(missing_ok=True)
            except OSError:
                pass
            LOG.info(
                "pipeline stopped; camera=%s run_id=%s frames=%d uptime=%.1fs",
                self.config["input"].get("camera", "unknown"),
                self.run_id,
                self.frame_count,
                time.monotonic() - self.started_at,
            )

    def _stop_after_duration(self) -> bool:
        self.loop.quit()
        return GLib.SOURCE_REMOVE


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--camera-id", type=str, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--worker-epoch", type=str, default=None)
    parser.add_argument("--duration", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gst.init(None)
    run_id = args.run_id or f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    config = load_config(args.config, args.camera_id)
    config.setdefault("runtime", {})["worker_epoch"] = (
        args.worker_epoch or f"worker-{uuid.uuid4().hex[:8]}"
    )
    pipeline = SafetyPipeline(config, args.config, run_id)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: pipeline.loop.quit())
    pipeline.run(args.duration)


if __name__ == "__main__":
    main()
