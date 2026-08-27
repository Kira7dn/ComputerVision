#!/usr/bin/env python3
"""Standalone DeepStream Safety pipeline.

RTSP input -> person detector/tracker -> function-specific analysis -> NVOSD
-> RTSP output. Smoking behavior is a state attached to a person bbox;
fire/smoke are camera-level environmental detections. The worker has no
dependency on another tracker runtime or its configuration.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import importlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from adapters.media.mock_input import wait_for_rtsp_video
from adapters.models.dms_engine import (
    DmsInferenceResult,
    select_dms_overlay_detections,
)
from adapters.notification.service import NotificationService
from adapters.persistence.evidence_repository import EvidenceStore
from application.analysis_scheduler import (
    AnalysisAdmissionGate,
    FrameResultGate,
    LatestSampleExecutor,
    as_function_result,
)
from application.function_registry import FUNCTION_REGISTRY, registration_for
from application.pipeline_compiler import compile_camera_plan
from bootstrap.config import load_config, load_raw_config

DEEPSTREAM_ROOT = os.environ.get("DEEPSTREAM_ROOT", "/opt/nvidia/deepstream/deepstream-7.1")
os.environ["GIO_USE_PROXY"] = "0"
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
_plugin_path = os.path.join(DEEPSTREAM_ROOT, "lib", "gst-plugins")
_library_paths = [
    os.path.join(DEEPSTREAM_ROOT, "lib"),
    _plugin_path,
    "/usr/local/lib/python3.10/dist-packages/tensorrt_libs",
    "/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib",
    "/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib",
]
os.environ["GST_PLUGIN_PATH"] = os.pathsep.join(
    _library_paths[1:] + ([os.environ["GST_PLUGIN_PATH"]] if os.environ.get("GST_PLUGIN_PATH") else [])
)
os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
    _library_paths + ([os.environ["LD_LIBRARY_PATH"]] if os.environ.get("LD_LIBRARY_PATH") else [])
)

import gi  # noqa: E402
import numpy as np  # noqa: E402
import zmq  # noqa: E402

from domain.contracts import AnalysisSample, FrameKey, FunctionResult  # noqa: E402
from domain.dms_events import DmsAlertEventStore  # noqa: E402
from domain.dms_health import resolve_dms_health  # noqa: E402
from domain.fire_smoke_events import FireSmokeEventStore  # noqa: E402
from domain.front_assistance import (  # noqa: E402
    FrontAlertTransition,
    FrontPerception,
    VisionAlertPolicy,
)
from domain.front_overlay import (  # noqa: E402
    build_front_hud,
    chunk_osd_items,
    project_front_overlay,
)
from domain.recognition import RecognitionCore, TrackKey  # noqa: E402
from domain.smoking_events import SmokingEpisodeStore, SmokingInferenceBatch  # noqa: E402
from domain.tracking import (  # noqa: E402
    PersonConfirmation,
    intersection_over_candidate,
    iou,
    opposite_frame_edge_transition,
    track_distance,
)

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

try:
    pyds = importlib.import_module("pyds")
except ModuleNotFoundError:  # The model-free E2E mock does not need DeepStream metadata.
    pyds = None  # type: ignore[assignment]

LOG = logging.getLogger("ls-vision")


def make_element(factory: str, name: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element is unavailable: {factory}")
    return element


def nms(boxes: list[np.ndarray], threshold: float) -> list[np.ndarray]:
    selected: list[np.ndarray] = []
    for box in sorted(boxes, key=lambda item: float(item[4]), reverse=True):
        if all(iou(box, other) <= threshold for other in selected):
            selected.append(box)
    return selected


def _dms_status_text(
    status: str,
    alerts: tuple[str, ...],
    message: str | None = None,
) -> tuple[str, int]:
    """Return a bounded two-line DMS HUD status and its line count."""
    if not alerts:
        if status in {"MONITORING", "OK"} or not message:
            return f"DMS {status}", 1
        detail = textwrap.shorten(message.upper(), width=42, placeholder="…")
        return f"DMS {status}\n{detail}", 2
    alert_text = " • ".join(str(alert) for alert in alerts)
    lines = textwrap.wrap(
        alert_text,
        width=38,
        break_long_words=False,
        break_on_hyphens=False,
    ) or ["-"]
    if len(lines) > 2:
        lines = [lines[0], f"{lines[1][:34].rstrip()}…"]
    return "DMS ALERT\n" + "\n".join(lines[:2]), 1 + min(2, len(lines))


def _ntp_latency_ms(ntp_timestamp: int, now: float) -> float | None:
    """Return camera-to-DeepStream latency from an NTP nanosecond timestamp."""
    if ntp_timestamp <= 0 or ntp_timestamp >= 2**63 - 1:
        return None
    latency_ms = (now - (ntp_timestamp / 1_000_000_000)) * 1000.0
    # A clock step or malformed metadata must not become a plausible dashboard
    # number. The camera and Jetson are expected to be NTP-synchronised.
    if latency_ms < -1_000.0 or latency_ms > 120_000.0:
        return None
    return latency_ms


class DeepStreamCameraRuntime:
    def __init__(self, config: dict[str, Any], config_path: Path, run_id: str) -> None:
        self.config = config
        self.config_path = config_path
        self.run_id = run_id
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.Pipeline.new("ls-vision")
        if self.pipeline is None:
            raise RuntimeError("Unable to create GStreamer pipeline")
        self.depay: Gst.Element | None = None
        self.person_infer: Gst.Element | None = None
        self.frame_probe_id: int | None = None
        self.started_at = time.monotonic()
        runtime = config.get("runtime", {}) or {}
        self.opencv_threads = max(1, int(runtime.get("opencv_threads", 1)))
        cv2_module = importlib.import_module("cv2")
        cv2_module.setNumThreads(self.opencv_threads)
        LOG.info("OpenCV worker threads: %d", cv2_module.getNumThreads())
        self.status_dir = Path(str(runtime.get("status_directory", "/opt/ls-vision/data/status")))
        self.status_path = self.status_dir / f"{config.get('input', {}).get('camera', 'camera')}.json"
        self.last_frame_at: float | None = None
        self.last_output_at: float | None = None
        self.last_output_pts_ns: int | None = None
        self.last_camera_latency_ms: float | None = None
        self.last_camera_source_timestamp: float | None = None
        self.camera_latency_samples = 0
        # Keep enough history to correlate a browser that is temporarily
        # buffered behind the live edge without guessing a frame pair.
        self._frame_timing_samples: deque[dict[str, float | int]] = deque(maxlen=360)
        self.execution_plan = compile_camera_plan(config)
        enabled_functions = set(self.execution_plan.enabled_functions)
        functions = config.get("functions", {}) or {}
        self.face_recognition_enabled = "face_recognition" in enabled_functions
        self.dms_enabled = "dms" in enabled_functions
        self.smoking_behavior_enabled = "smoking_behavior" in enabled_functions
        self.fire_smoke_enabled = "fire_smoke" in enabled_functions
        self.front_assistance_enabled = "front_assistance" in enabled_functions
        self.person_inference_enabled = (
            "person_inference" in self.execution_plan.shared_nodes
        )
        self.trace_enabled = bool(functions.get("trace", True))
        LOG.info(
            "function topology: camera=%s dms=%s face_recognition=%s smoking_behavior=%s fire_smoke=%s front_assistance=%s person_inference=%s",
            config.get("input", {}).get("camera", "unknown"),
            self.dms_enabled,
            self.face_recognition_enabled,
            self.smoking_behavior_enabled,
            self.fire_smoke_enabled,
            self.front_assistance_enabled,
            self.person_inference_enabled,
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
        self.event_store = SmokingEpisodeStore(config, self.evidence)
        self.dms_events = DmsAlertEventStore(config, self.evidence)
        self.fire_smoke_events = FireSmokeEventStore(config, self.evidence)
        self._face_event_ids: dict[int, str] = {}
        self._smoking_by_track: dict[int, Any] = {}
        self.mock_publisher: subprocess.Popen | None = None
        # The per-camera function map is authoritative. This prevents a global
        # recognition default from loading face models in cameras that did not
        # request the function, while allowing a camera override to enable it.
        config.setdefault("recognition", {})["enabled"] = self.face_recognition_enabled
        self.recognition = RecognitionCore(config)
        for registration in FUNCTION_REGISTRY.values():
            setattr(self, registration.engine_attribute, None)
        for spec in self.execution_plan.functions:
            registration = registration_for(spec.name)
            setattr(
                self,
                registration.engine_attribute,
                self._create_function_engine(spec.name),
            )
        # CPU frame decoding currently lives in the face adapter. Construct its
        # disabled, model-free form when another function needs CPU frames.
        if self.face_engine is None:
            self.face_engine = self._create_function_engine("face_recognition")
        front_config = config.get("front_assistance", {}) or {}
        overlay_config = front_config.get("overlay", {}) or {}
        self.front_policy = VisionAlertPolicy(
            config=front_config.get("alerts", {}) or {},
            path_half_width_m=float(overlay_config.get("path_half_width_m", 0.9)),
            max_gap_seconds=float(front_config.get("max_gap_seconds", 0.25)),
        )
        self._front_perception: FrontPerception | None = None
        self._front_transitions: list[FrontAlertTransition] = []
        self._front_overlay_metrics: dict[str, Any] = {
            "visible_lane_count": 0,
            "lane_segment_count": 0,
            "path_point_count": 0,
            "path_segment_count": 0,
            "visible_road_edge_count": 0,
            "road_edge_segment_count": 0,
            "visible_lead_count": 0,
            "lead_segment_count": 0,
            "lead_chevron_count": 0,
            "lead_style": "openpilot_chevron",
            "horizon_marker_count": 0,
            "rendered_segment_count": 0,
            "lane_confidences": {},
        }
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
        self._person_confirmation_hits = int(
            tracking_config.get("confirmation_hits", 2)
        )
        self._person_confirmation_window = int(
            tracking_config.get("confirmation_window", 4)
        )
        self.last_behavior_error: str | None = None
        self.analysis_enabled = "cpu_frame" in self.execution_plan.shared_nodes
        self._analysis_lock = threading.RLock()
        self._analysis_detections: list[Any] = []
        self._analysis_dms_detections: list[Any] = []
        self._analysis_dms_result: DmsInferenceResult | None = None
        self._analysis_fire_smoke: list[Any] = []
        self._analysis_transitions: list[Any] = []
        self._analysis_transitions_by_function: dict[str, list[Any]] = {}
        self._analysis_results_by_function: dict[str, FunctionResult] = {}
        self._analysis_last_transition: str | None = None
        self._analysis_frame_num: int | None = None
        self._analysis_updated_at_by_function: dict[str, float] = {}
        analysis_intervals = [
            float(engine.interval_seconds)
            for engine in (
                self.dms_engine,
                self.smoking_behavior_engine,
                self.fire_smoke_engine,
                self.front_engine,
            )
            if engine is not None
        ]
        if self.face_engine.enabled:
            analysis_intervals.append(
                self.face_engine.recognition_scheduler.interval_seconds
            )
        self._analysis_interval_seconds = min(analysis_intervals, default=0.5)
        self._analysis_admission_gate = AnalysisAdmissionGate(
            self._analysis_interval_seconds
        )
        self._analysis_result_max_age_seconds = max(
            1.0,
            float(
                runtime.get(
                    "analysis_result_max_age_seconds",
                    max(2.0, self._analysis_interval_seconds * 4.0),
                )
            ),
        )
        self._analysis_gate = FrameResultGate(self._analysis_result_max_age_seconds)
        self._analysis_executors: dict[str, LatestSampleExecutor] = {}
        self._analysis_error: str | None = None
        self._analysis_stale_drops = 0
        self._analysis_out_of_order_drops = 0
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
        self._last_person_fire_smoke_overlap_count = 0
        self._metadata_write_at = 0.0
        self._metadata_write_interval_seconds = max(
            0.10,
            float(runtime.get("live_metadata_interval_ms", 250)) / 1000.0,
        )
        self.metadata_path = self.status_dir / f"{config.get('input', {}).get('camera', 'camera')}.metadata.json"
        self.socket = zmq.Context.instance().socket(zmq.PUB)
        self.socket.bind(config["metadata"]["zmq_pub_url"])
        self.person_infer_config = (
            self._write_person_infer_config() if self.person_inference_enabled else None
        )
        self._build()
        if self.analysis_enabled:
            self._start_analysis_workers()

    def _on_face_trace(
        self, track_id: int, data: dict[str, Any], frame: np.ndarray | None
    ) -> None:
        event_name = str(data.get("event", "update"))
        event_id = self._face_event_ids.get(track_id)
        stable_name = str(data.get("stable_result") or "unknown")
        # A tracked person is not a recognition event. Create the event only
        # when the identity becomes stable, using the exact frame that
        # produced that recognition result as the START report image.
        if event_id is None and stable_name != "unknown":
            recognition_frame = data.get("frame")
            person_bbox = data.get("person_bbox")
            event_id = self.evidence.start_event(
                event_id=(
                    f"face-{self.run_id}-{self.config['input']['camera']}"
                    f"-{self.evidence.worker_epoch}-{track_id}"
                ),
                function="face_recognition",
                classification="recognized",
                camera_id=str(self.config["input"]["camera"]),
                person_track_id=track_id,
                metadata={
                    "identity": stable_name,
                    "recognition_frame_number": recognition_frame,
                    "recognition_source_timestamp": data.get("ts"),
                    "face_bbox": data.get("face_bbox"),
                    "person_bbox": person_bbox,
                },
                frame=frame,
                frame_number=(
                    int(recognition_frame) if recognition_frame is not None else None
                ),
                bbox=tuple(person_bbox) if person_bbox else None,
                score=float(data.get("stable_score", data.get("score", 0.0)) or 0.0),
            )
            self._face_event_ids[track_id] = event_id
            self._notify_event(event_id, "START")
            return
        if event_name == "track_end" and event_id is None:
            final_name = str(data.get("name") or "unknown")
            evidence_frame = data.get("evidence_frame")
            evidence_person_bbox = data.get("evidence_person_bbox")
            if (
                final_name == "unknown"
                and frame is not None
                and evidence_frame is not None
                and data.get("evidence_face_bbox")
                and float(data.get("evidence_face_score") or 0.0)
                >= float(self.face_engine.detector_threshold)
            ):
                event_id = (
                    f"face-unknown-{self.run_id}-{self.config['input']['camera']}"
                    f"-{self.evidence.worker_epoch}-{track_id}-{evidence_frame}-"
                    f"{uuid.uuid4().hex[:8]}"
                )
                self.evidence.start_event(
                    event_id=event_id,
                    function="face_recognition",
                    classification="unrecognized",
                    camera_id=str(self.config["input"]["camera"]),
                    person_track_id=track_id,
                    metadata={
                        "identity": "unknown",
                        "recognition_frame_number": evidence_frame,
                        "recognition_source_timestamp": data.get(
                            "evidence_source_timestamp"
                        ),
                        "evidence_quality": data.get("evidence_quality"),
                        "evidence_face_score": data.get("evidence_face_score"),
                        "face_bbox": data.get("evidence_face_bbox"),
                        "person_bbox": evidence_person_bbox,
                    },
                    frame=frame,
                    frame_number=int(evidence_frame),
                    bbox=tuple(evidence_person_bbox)
                    if evidence_person_bbox
                    else None,
                    score=None,
                )
                self._notify_event(event_id, "START")
                self.evidence.finish_event(
                    event_id,
                    classification="unrecognized",
                    identity="unknown",
                    payload={"event": "unknown_track_end", **data},
                    frame=None,
                    frame_number=int(evidence_frame),
                    bbox=tuple(evidence_person_bbox)
                    if evidence_person_bbox
                    else None,
                    score=None,
                )
            return
        if event_name == "track_start" or event_id is None:
            return
        if event_name == "track_end":
            final_name = str(data.get("name") or "unknown")
            self.evidence.finish_event(
                event_id,
                classification="recognized" if final_name != "unknown" else "unrecognized",
                identity=None if final_name == "unknown" else final_name,
                payload=data,
                frame=None,
                frame_number=int(data.get("frame", -1)),
                bbox=tuple(data.get("person_bbox", [])) if data.get("person_bbox") else None,
                score=float(data.get("score", 0.0)),
            )
            self._face_event_ids.pop(track_id, None)
            return
        self.evidence.record(
            event_id,
            "UPDATE",
            {**data, "identity": None if stable_name == "unknown" else stable_name},
            frame=frame,
            frame_number=int(data.get("frame", -1)),
            bbox=tuple(data.get("person_bbox", [])) if data.get("person_bbox") else None,
            score=float(data.get("stable_score", data.get("score", 0.0))),
        )

        return

    def _notify_event(self, event_id: str, lifecycle: str) -> None:
        """Queue only an artifact-backed event-start notification."""
        if lifecycle != "START":
            return
        self.notifications.notify_event(
            event_id,
            lifecycle,
            self.evidence.event_directory(event_id),
        )

    def _notify_transitions(self, transitions: list[Any]) -> None:
        for transition in transitions:
            operation = str(getattr(transition, "operation", ""))
            if operation == "NOTIFY":
                self._notify_event(str(transition.event_id), "START")

    def _write_person_infer_config(self) -> str:
        model = self.config["person"]
        model_path = os.path.abspath(model["onnx_path"])
        engine_path = str(model.get("engine_path", "/opt/ls-vision/models/person-yolov9-t320.engine"))
        if not Path(engine_path).is_file() and bool(
            (self.config.get("runtime", {}) or {}).get("allow_engine_build", False)
        ):
            Path(engine_path).parent.mkdir(parents=True, exist_ok=True)
        model_source = (
            f"model-engine-file={engine_path}"
            if Path(engine_path).is_file()
            else f"onnx-file={model_path}\nmodel-engine-file={engine_path}"
        )
        content = f"""[property]
gpu-id=0
{model_source}
labelfile-path=/tmp/ls-vision-person-labels.txt
batch-size=1
network-mode=2
network-type=100
model-color-format=0
net-scale-factor=0.00392156862745098
num-detected-classes=80
infer-dims=3;{int(model['input_width'])};{int(model['input_height'])}
interval={max(0, int(model.get('inference_interval', 0)))}
gie-unique-id=2
process-mode=1
output-tensor-meta=1
maintain-aspect-ratio=0
"""
        Path("/tmp/ls-vision-person-labels.txt").write_text("person\n", encoding="utf-8")
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix="ls-vision-person-infer-", suffix=".txt", delete=False
        )
        with handle:
            handle.write(content)
        LOG.info("person nvinfer config: %s", handle.name)
        return handle.name

    @staticmethod
    def _configure_output_payloader(_sink: Gst.Element, payloader: Gst.Element) -> None:
        """Configure the payloader created internally by rtspclientsink."""
        try:
            payloader.set_property("timestamp-offset", 0)
            payloader.set_property("perfect-rtptime", True)
            LOG.info("output payloader RTP timestamp mapping configured")
        except (TypeError, AttributeError):
            LOG.debug("output payloader does not expose deterministic timestamp properties")

    def _build(self) -> None:
        input_cfg = self.config["input"]
        output_cfg = self.config["output"]
        publish_video = bool(output_cfg.get("publish_video", True))
        self.output_video_published = publish_video
        output_width = int(output_cfg.get("width", input_cfg["width"]))
        output_height = int(output_cfg.get("height", input_cfg["height"]))
        output_bitrate_bps = int(output_cfg.get("bitrate_bps", 4_000_000))
        output_rate_hz = float(output_cfg.get("rate_hz", 0.0) or 0.0)
        if output_width <= 0 or output_height <= 0:
            raise ValueError("output width and height must be positive")
        if output_bitrate_bps <= 0:
            raise ValueError("output bitrate_bps must be positive")
        self.output_resolution = {"width": output_width, "height": output_height}
        self.output_rate_hz = output_rate_hz or None
        self._output_admission_gate = (
            AnalysisAdmissionGate(1.0 / output_rate_hz)
            if publish_video and output_rate_hz > 0.0
            else None
        )

        source = make_element("rtspsrc", "rtsp-source")
        source.set_property("location", input_cfg["rtsp_url"])
        if input_cfg.get("rtsp_username"):
            source.set_property("user-id", input_cfg["rtsp_username"])
        if input_cfg.get("rtsp_password"):
            source.set_property("user-pw", input_cfg["rtsp_password"])
        source.set_property("latency", int(input_cfg["latency_ms"]))
        source.set_property("protocols", 4)
        # Preserve the camera's RTCP Sender Report/NTP mapping so NvDsFrameMeta
        # can carry the capture timestamp instead of only a local arrival PTS.
        source.set_property("ntp-sync", True)
        codec = str(input_cfg.get("codec", "h264")).lower()
        if codec in {"h265", "hevc"}:
            depay = make_element("rtph265depay", "rtp-h265-depay")
            parser = make_element("h265parse", "input-h265-parse")
        else:
            depay = make_element("rtph264depay", "rtp-h264-depay")
            parser = make_element("h264parse", "input-h264-parse")
        self.depay = depay
        software_decode = str(input_cfg.get("decoder", "hardware")).lower() in {
            "software",
            "cpu",
        }
        decoder_factory = (
            ("avdec_h265" if codec in {"h265", "hevc"} else "avdec_h264")
            if software_decode
            else "nvv4l2decoder"
        )
        self.input_decoder = decoder_factory
        self.input_mirror_horizontal = bool(input_cfg.get("mirror_horizontal", False))
        decoder = make_element(decoder_factory, "input-decoder")
        input_transform = None
        input_transform_caps = None
        if software_decode or self.input_mirror_horizontal:
            input_transform = make_element("nvvideoconvert", "input-transform")
            if self.input_mirror_horizontal:
                input_transform.set_property("flip-method", 4)
                LOG.info("input horizontal mirror enabled before nvstreammux")
            input_transform_caps = make_element("capsfilter", "input-transform-caps")
            input_transform_caps.set_property(
                "caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12")
            )
        mux = make_element("nvstreammux", "stream-muxer")
        mux.set_property("batch-size", 1)
        mux.set_property("width", int(input_cfg["width"]))
        mux.set_property("height", int(input_cfg["height"]))
        mux.set_property("live-source", 1)
        mux.set_property("batched-push-timeout", 40000)

        if self.person_inference_enabled:
            self.person_infer = make_element("nvinfer", "person-inference")
            self.person_infer.set_property(
                "config-file-path", self.person_infer_config
            )
        analysis_tee = make_element("tee", "analysis-tee")
        analysis_queue = make_element("queue", "analysis-queue")
        analysis_queue.set_property("max-size-buffers", 1)
        analysis_queue.set_property("max-size-bytes", 0)
        analysis_queue.set_property("max-size-time", 0)
        analysis_queue.set_property("leaky", 2)
        output_input_queue = None
        if publish_video:
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
        output_elements: list[Gst.Element] = []
        if publish_video:
            convert_before_osd = make_element("nvvideoconvert", "convert-before-osd")
            face_rgba_caps = make_element("capsfilter", "face-rgba-caps")
            face_rgba_caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=RGBA"))
            osd = make_element("nvdsosd", "bbox-osd")
            osd.set_property("display-bbox", True)
            osd.set_property("display-text", True)
            convert_after_osd = make_element("nvvideoconvert", "convert-after-osd")
            output_tee = make_element("tee", "output-tee")
            output_queue = make_element("queue", "output-queue")
            # Keep the output branch latest-frame bounded so a slow network reader
            # cannot turn the live RTSP publication into historical playback.
            output_queue.set_property("max-size-buffers", 2)
            output_queue.set_property("max-size-bytes", 0)
            output_queue.set_property("max-size-time", 0)
            output_queue.set_property("leaky", 2)
            try:
                encoder = make_element("nvv4l2h264enc", "output-encoder")
                encoder.set_property("bitrate", output_bitrate_bps)
                encoder.set_property("iframeinterval", 15)
                encoder.set_property("idrinterval", 15)
                encoder.set_property("preset-id", 1)
                for property_name, property_value in (("insert-sps-pps", 1), ("num-B-Frames", 0)):
                    try:
                        encoder.set_property(property_name, property_value)
                    except (TypeError, AttributeError):
                        LOG.debug("output encoder does not expose property=%s", property_name)
                output_caps = (
                    "video/x-raw(memory:NVMM),format=I420,"
                    f"width={output_width},height={output_height}"
                )
                self.output_encoder = "nvv4l2h264enc"
                LOG.info("output encoder: nvv4l2h264enc")
            except RuntimeError:
                encoder = make_element("x264enc", "output-encoder")
                encoder.set_property("bitrate", max(1, output_bitrate_bps // 1_000))
                encoder.set_property("speed-preset", "ultrafast")
                encoder.set_property("tune", "zerolatency")
                encoder.set_property("key-int-max", 15)
                encoder.set_property("bframes", 0)
                output_caps = (
                    "video/x-raw,format=I420,"
                    f"width={output_width},height={output_height}"
                )
                self.output_encoder = "x264enc"
                LOG.warning("output encoder: x264enc fallback")
            output_i420_caps = make_element("capsfilter", "output-i420-caps")
            output_i420_caps.set_property(
                "caps",
                Gst.Caps.from_string(output_caps),
            )
            output_parser = make_element("h264parse", "output-h264-parse")
            output_parser.set_property("config-interval", 1)
            sink = make_element("rtspclientsink", "rtsp-output")
            sink.set_property("location", output_cfg["rtsp_url"])
            sink.set_property("protocols", 4)
            sink.set_property("latency", 100)
            try:
                sink.connect("new-payloader", self._configure_output_payloader)
            except (TypeError, AttributeError):
                LOG.debug("rtspclientsink does not expose new-payloader")
            output_elements = [
                convert_before_osd,
                face_rgba_caps,
                osd,
                convert_after_osd,
                output_i420_caps,
                output_tee,
                output_queue,
                encoder,
                output_parser,
                sink,
            ]
        else:
            self.output_encoder = "disabled"
            LOG.info("output encoder disabled: metadata-only worker")

        elements = [
            source,
            self.depay,
            parser,
            decoder,
            *(
                [input_transform, input_transform_caps]
                if input_transform is not None
                else []
            ),
            mux,
            *([self.person_infer] if self.person_infer is not None else []),
            analysis_tee,
            analysis_queue,
            *([output_input_queue] if output_input_queue is not None else []),
            *(
                [face_cpu_convert, face_cpu_caps]
                if self.face_engine.enabled
                or self.dms_enabled
                or self.smoking_behavior_enabled
                or self.fire_smoke_enabled
                or self.front_assistance_enabled
                else []
            ),
            analysis_sink,
            *output_elements,
        ]
        # PyGObject exposes Gst.Bin.add() as a single-element call on the
        # DeepStream Jetson image; add the topology one element at a time.
        for element in elements:
            self.pipeline.add(element)
        source.connect("pad-added", self._on_source_pad_added)
        if not self.depay.link(parser) or not parser.link(decoder):
            raise RuntimeError("Unable to link RTSP depayloader to decoder")
        decoder_src = decoder.get_static_pad("src")
        if input_transform is not None:
            if not decoder.link(input_transform) or not input_transform.link(input_transform_caps):
                raise RuntimeError("Unable to transform decoder output before nvstreammux")
            decoder_src = input_transform_caps.get_static_pad("src")
        mux_sink = mux.get_request_pad("sink_0")
        if decoder_src is None or mux_sink is None or decoder_src.link(mux_sink) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Unable to link decoder to nvstreammux")
        primary_analysis = mux
        if self.person_infer is not None:
            if not mux.link(self.person_infer):
                raise RuntimeError("Unable to link nvstreammux to person nvinfer")
            primary_analysis = self.person_infer
        if not primary_analysis.link(analysis_tee):
            raise RuntimeError("Unable to link primary inference to analysis tee")
        analysis_tee_pad = analysis_tee.get_request_pad("src_%u")
        analysis_sink_pad = analysis_queue.get_static_pad("sink")
        if (
            analysis_tee_pad is None
            or analysis_sink_pad is None
            or analysis_tee_pad.link(analysis_sink_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError("Unable to link analysis tee to analysis queue")
        if publish_video:
            output_tee_pad = analysis_tee.get_request_pad("src_%u")
            output_sink_pad = output_input_queue.get_static_pad("sink")
            if (
                output_tee_pad is None
                or output_sink_pad is None
                or output_tee_pad.link(output_sink_pad) != Gst.PadLinkReturn.OK
            ):
                raise RuntimeError("Unable to link analysis tee to output queue")
        needs_cpu_frame = (
            self.face_engine.enabled
            or self.dms_enabled
            or self.smoking_behavior_enabled
            or self.fire_smoke_enabled
            or self.front_assistance_enabled
        )
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
        if publish_video:
            if not output_input_queue.link(convert_before_osd):
                raise RuntimeError("Unable to link output queue to OSD converter")
            if not convert_before_osd.link(face_rgba_caps):
                raise RuntimeError("Unable to link CPU face frame to OSD converter")
            if not face_rgba_caps.link(osd) or not osd.link(convert_after_osd):
                raise RuntimeError("Unable to link OSD branch")
            if not convert_after_osd.link(output_i420_caps):
                raise RuntimeError("Unable to link post-OSD output caps")
            if not output_i420_caps.link(output_tee):
                raise RuntimeError("Unable to link output tee")
            if not output_tee.link(output_queue):
                raise RuntimeError("Unable to link output branches")
            if not output_queue.link(encoder) or not encoder.link(output_parser):
                raise RuntimeError("Unable to link output encoder")
            if self._output_admission_gate is not None:
                output_admission_src = output_queue.get_static_pad("src")
                if output_admission_src is None:
                    raise RuntimeError("Output queue has no src pad")
                output_admission_src.add_probe(
                    Gst.PadProbeType.BUFFER,
                    self._on_output_admission_buffer,
                )
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
        metadata_src = primary_analysis.get_static_pad("src")
        if metadata_src is None:
            raise RuntimeError("Primary analysis element has no src pad")
        if self.person_infer is not None:
            # Create person objects before assigning stable application track IDs;
            # otherwise behavior and face recognition see every object as UNTRACKED.
            metadata_src.add_probe(Gst.PadProbeType.BUFFER, self._on_person_buffer)
        metadata_src.add_probe(Gst.PadProbeType.BUFFER, self._on_metadata_buffer)
        face_src = analysis_src.get_static_pad("src")
        if face_src is None:
            raise RuntimeError("Analysis probe has no src pad")
        self.frame_probe_id = face_src.add_probe(
            Gst.PadProbeType.BUFFER, self._on_behavior_buffer
        )
        if needs_cpu_frame:
            analysis_admission_src = analysis_queue.get_static_pad("src")
            if analysis_admission_src is None:
                raise RuntimeError("Analysis admission queue has no src pad")
            analysis_admission_src.add_probe(
                Gst.PadProbeType.BUFFER,
                self._on_analysis_admission_buffer,
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
            scale_x = frame_width / input_width
            scale_y = frame_height / input_height
            selected = matrix[matrix[:, 4] >= threshold, :5].astype(
                np.float32,
                copy=False,
            )
            if selected.size == 0:
                return []
            centers = selected[:, :2]
            sizes = selected[:, 2:4]
            top_left = (centers - sizes / 2.0) * np.array(
                [scale_x, scale_y],
                dtype=np.float32,
            )
            bottom_right = (centers + sizes / 2.0) * np.array(
                [scale_x, scale_y],
                dtype=np.float32,
            )
            top_left = np.maximum(top_left, 0.0)
            bottom_right = np.minimum(
                bottom_right,
                np.array([float(frame_width), float(frame_height)], dtype=np.float32),
            )
            valid = np.all(bottom_right > top_left, axis=1)
            packed = np.column_stack(
                (top_left[valid], bottom_right[valid], selected[valid, 4])
            ).astype(np.float32, copy=False)
            boxes = [row for row in packed]
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

    def _start_analysis_workers(self) -> None:
        camera_id = str(self.config["input"].get("camera", "camera"))
        for spec in self.execution_plan.functions:
            registration = registration_for(spec.name)
            engine = getattr(self, registration.engine_attribute, None)
            if engine is None:
                raise RuntimeError(
                    f"camera {camera_id} function {spec.name} has no runtime engine"
                )
            executor = LatestSampleExecutor(
                name=spec.name,
                interval_seconds=spec.interval_seconds,
                processor=getattr(self, registration.processor_method),
                on_result=self._on_analysis_result,
                on_error=self._on_analysis_error,
            )
            self._analysis_executors[spec.name] = executor
            executor.start()
        LOG.info(
            "analysis executors started: camera=%s functions=%s",
            camera_id,
            sorted(self._analysis_executors),
        )

    def _create_function_engine(self, function: str) -> Any:
        registration = registration_for(function)
        module = importlib.import_module(registration.engine_module)
        engine_class = getattr(module, registration.engine_class)
        if registration.passes_trace_sink:
            return engine_class(self.config, self._on_face_trace)
        return engine_class(self.config)

    def _process_face_sample(self, sample: AnalysisSample) -> dict[int, dict[str, Any]]:
        return self.face_engine.process_frame(
            sample.frame,
            list(sample.persons),
            sample.key.frame_number,
        )

    def _process_smoking_sample(self, sample: AnalysisSample) -> SmokingInferenceBatch:
        if self.smoking_behavior_engine is None:
            return SmokingInferenceBatch((), ())
        return self.smoking_behavior_engine.process(
            sample.frame,
            list(sample.persons),
            sample.key.frame_number,
        )

    def _process_dms_sample(self, sample: AnalysisSample) -> DmsInferenceResult:
        if self.dms_engine is None:
            return DmsInferenceResult(SmokingInferenceBatch((), ()), (), (), "DISABLED", {})
        return self.dms_engine.process(
            sample.frame,
            list(sample.persons),
            sample.key.frame_number,
            source_timestamp=sample.source_timestamp,
        )

    def _process_fire_smoke_sample(self, sample: AnalysisSample) -> dict[str, Any]:
        if self.fire_smoke_engine is None:
            return {"detections": [], "fresh": [], "inference_ran": False}
        detections = self.fire_smoke_engine.process(sample.frame)
        return {
            "detections": list(detections),
            "fresh": list(self.fire_smoke_engine.last_fresh_detections),
            "inference_ran": bool(self.fire_smoke_engine.last_inference_ran),
        }

    def _process_front_sample(self, sample: AnalysisSample) -> FrontPerception:
        if self.front_engine is None:
            raise RuntimeError("front assistance engine is unavailable")
        return self.front_engine.process(
            sample.frame,
            source_epoch=self.evidence.worker_epoch,
            frame_number=sample.key.frame_number,
            source_timestamp=sample.source_timestamp,
        )

    def _record_front_transitions(
        self,
        transitions: list[FrontAlertTransition],
        sample: AnalysisSample,
        perception: FrontPerception,
    ) -> None:
        common = {
            "mode": "vision_only",
            "source_epoch": perception.source_epoch,
            "source_timestamp": perception.source_timestamp,
            "model_hash": perception.model_hash,
            "calibration_hash": perception.calibration_hash,
            "provider": perception.provider,
        }
        for transition in transitions:
            payload = {**common, **transition.metadata}
            if transition.operation == "START":
                self.evidence.start_event(
                    event_id=transition.event_id,
                    function="front_assistance",
                    classification=transition.label,
                    camera_id=str(self.config["input"]["camera"]),
                    metadata=payload,
                    frame=sample.frame,
                    frame_number=sample.key.frame_number,
                    score=transition.confidence,
                )
            elif transition.operation == "END":
                self.evidence.finish_event(
                    transition.event_id,
                    classification=transition.label,
                    payload=payload,
                    frame=sample.frame,
                    frame_number=sample.key.frame_number,
                    score=transition.confidence,
                )

    def _on_analysis_error(self, function: str, exc: Exception) -> None:
        message = f"{function}: {exc}"
        if message != self._analysis_error:
            LOG.exception("background analysis failed: %s", message, exc_info=exc)
            self._analysis_error = message

    def _on_analysis_result(
        self,
        function: str,
        sample: AnalysisSample,
        raw_result: Any,
        started: float,
        finished: float,
    ) -> None:
        decision = self._analysis_gate.evaluate(function, sample, finished)
        if not decision.accepted:
            if decision.reason == "stale":
                self._analysis_stale_drops += 1
            else:
                self._analysis_out_of_order_drops += 1
            LOG.warning(
                "analysis result dropped: camera=%s function=%s frame=%d reason=%s age=%.3fs",
                sample.key.camera_id,
                function,
                sample.key.frame_number,
                decision.reason,
                decision.age_seconds,
            )
            return

        detections: list[Any] = []
        transitions: list[Any] = []
        metadata: dict[str, Any] = {"result_age_seconds": decision.age_seconds}
        if function == "face_recognition":
            face_tracks = dict(raw_result)
            detections = list(face_tracks.values())
            with self._analysis_lock:
                self._face_tracks = face_tracks
        elif function == "dms":
            transitions.extend(
                self.dms_events.observe(
                    frame_num=sample.key.frame_number,
                    timestamp=sample.source_timestamp,
                    result=raw_result,
                    frame=sample.frame,
                )
            )
            confirmed_alerts = tuple(self.dms_events.active_labels)
            confirmed_metrics = {
                **dict(raw_result.metrics),
                "active_alerts": list(confirmed_alerts),
                "candidate_alerts": self.dms_events.candidate_labels,
                "event_lifecycle": self.dms_events.metrics(),
            }
            health = resolve_dms_health(
                raw_result.status,
                confirmed_alerts,
                confirmed_metrics,
                raw_result.message,
            )
            confirmed_status = health.status
            confirmed_metrics.update(
                {
                    "observation_ready": health.observation_ready,
                    "driver_visible": health.driver_visible,
                    "face_visible": health.face_visible,
                }
            )
            published_result = DmsInferenceResult(
                raw_result.smoking,
                raw_result.detections,
                confirmed_alerts,
                confirmed_status,
                confirmed_metrics,
                health.message,
            )
            confirmed_detections = select_dms_overlay_detections(
                item
                for item in raw_result.detections
                if item.label in confirmed_alerts
            )
            metadata.update(
                {
                    "status": confirmed_status,
                    "alerts": list(confirmed_alerts),
                    "candidate_alerts": self.dms_events.candidate_labels,
                    "metrics": confirmed_metrics,
                    "message": health.message,
                    "raw_detection_count": len(raw_result.detections),
                    "confirmed_detection_count": len(confirmed_detections),
                }
            )
            # DMS owns its complete lifecycle. Do not also feed DMS Smoking
            # observations into the independent SmokingEpisodeStore.
            detections = []
            with self._analysis_lock:
                self._analysis_dms_detections = confirmed_detections
                self._analysis_dms_result = published_result
        elif function == "smoking_behavior":
            metadata["raw_observation_count"] = len(raw_result.observations)
            metadata["invalid_crop_count"] = len(raw_result.invalid_crop_track_ids)
            transitions.extend(self.event_store.observe(
                frame_num=sample.key.frame_number,
                timestamp=sample.source_timestamp,
                observations=list(raw_result.observations),
                observed_track_ids=set(raw_result.observed_track_ids),
                frame=sample.frame,
                invalid_crop_track_ids=set(raw_result.invalid_crop_track_ids),
            ))
            detections = list(self.event_store.visible_detections)
        elif function == "fire_smoke":
            metadata["inference_ran"] = bool(raw_result["inference_ran"])
            metadata["raw_detection_count"] = len(raw_result["fresh"])
            if raw_result["inference_ran"]:
                transitions.extend(
                    self.fire_smoke_events.observe(
                        frame_num=sample.key.frame_number,
                        timestamp=sample.source_timestamp,
                        detections=list(raw_result["fresh"]),
                        frame=sample.frame,
                    )
                )
            # Raw/cached detector output is diagnostic only. Only a verified
            # region may reach NVOSD, live metadata, events, or notifications.
            detections = list(self.fire_smoke_events.visible_detections)
        elif function == "front_assistance":
            perception = raw_result
            transitions.extend(self.front_policy.observe(perception))
            self._record_front_transitions(transitions, sample, perception)
            overlay_metrics, _segments = self._front_geometry_segments(perception)
            metadata.update(
                {
                    **perception.summary(),
                    "active_alerts": list(self.front_policy.active_labels),
                    "geometry_diagnostics": self.front_policy.geometry_diagnostics,
                    "overlay": overlay_metrics,
                    "transitions": [
                        {
                            "operation": item.operation,
                            "event_id": item.event_id,
                            "label": item.label,
                            "frame_number": item.frame_number,
                        }
                        for item in transitions
                    ],
                }
            )
            with self._analysis_lock:
                self._front_perception = perception
                self._front_transitions = list(transitions)
                self._front_overlay_metrics = overlay_metrics

        if function != "front_assistance":
            self._notify_transitions(transitions)
        model_section = "smoking_behavior" if function == "smoking_behavior" else function
        model_config = self.config.get(model_section, {}) or {}
        model_revision = str(
            model_config.get("model_path" if function == "front_assistance" else "onnx_path", "")
        ) or None
        result = as_function_result(
            function,
            sample,
            detections,
            started,
            finished,
            transitions=transitions,
            model_revision=model_revision,
            metadata=metadata,
        )
        with self._analysis_lock:
            self._analysis_results_by_function[function] = result
            self._analysis_transitions_by_function[function] = transitions
            self._analysis_transitions = [
                transition
                for function_transitions in self._analysis_transitions_by_function.values()
                for transition in function_transitions
            ]
            if function == "smoking_behavior":
                self._analysis_detections = detections
                self.last_bbox_count = len(detections)
            elif function == "fire_smoke":
                self._analysis_fire_smoke = detections
                self.last_fire_smoke_count = len(detections)
            self._analysis_frame_num = sample.key.frame_number
            self._analysis_updated_at_by_function[function] = finished
            self._analysis_last_transition = (
                transitions[-1].operation if transitions else self._analysis_last_transition
            )
            self._analysis_processed_count += 1
            self._analysis_last_processed_frame = sample.key.frame_number

    def _queue_analysis_sample(
        self,
        frame_meta: Any,
        frame: np.ndarray | None,
        persons: list[tuple[int, float, float, float, float]],
        now: float,
    ) -> None:
        if frame is None or not self._analysis_executors:
            return
        # _decode_bgrx_buffer() already returns an owned copy detached from the
        # GstBuffer. Share that immutable array with all latest-only executors
        # instead of copying the full frame for a second time.
        frame.setflags(write=False)
        raw_pts = int(getattr(frame_meta, "buf_pts", -1))
        buffer_pts_ns = raw_pts if 0 <= raw_pts < 2**63 - 1 else None
        ntp_timestamp = int(getattr(frame_meta, "ntp_timestamp", 0) or 0)
        sample = AnalysisSample(
            key=FrameKey(
                run_id=self.run_id,
                camera_id=str(self.config["input"].get("camera", "camera")),
                source_id=int(getattr(frame_meta, "source_id", 0)),
                frame_number=int(frame_meta.frame_num),
                buffer_pts_ns=buffer_pts_ns,
            ),
            source_timestamp=(ntp_timestamp / 1_000_000_000 if ntp_timestamp > 0 else time.time()),
            captured_monotonic=now,
            frame=frame,
            persons=tuple(persons),
        )
        submitted = 0
        for executor in self._analysis_executors.values():
            submitted += int(executor.submit(sample, now))
        if submitted:
            self._analysis_enqueued_count += submitted
            self._analysis_last_enqueued_frame = sample.key.frame_number

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
        dms_detections: list[Any],
        dms_result: DmsInferenceResult | None,
    ) -> None:
        """Render cached inference results on the independent live-output branch."""
        if self.dms_enabled:
            self._attach_dms_objects(batch_meta, frame_meta, dms_detections)
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
                if self.dms_enabled:
                    # DMS is the public overlay contract for the DMS camera.
                    # DeepStream still tracks persons internally for frame
                    # alignment and smoking event correlation, but the generic
                    # person detector box is not part of dms.py output.
                    obj_meta.rect_params.border_width = 0
                    obj_meta.text_params.display_text = ""
                    obj_meta.text_params.set_bg_clr = 0
                    continue
                if not self._person_track_confirmed(int(obj_meta.object_id)):
                    continue
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
        if self.dms_enabled and dms_result is not None:
            status_label, status_lines = _dms_status_text(
                dms_result.status,
                tuple(dms_result.alerts),
                dms_result.message,
            )
            frame_width = int(self.config["input"].get("width", 1920))
            frame_height = int(self.config["input"].get("height", 1080))
            dms_label_positions: list[tuple[str, int, int]] = []
            reserved_top = max(112, 24 + status_lines * 38 + 16)
            for detection in dms_detections:
                label = f"{detection.label} {float(detection.score) * 100:.0f}%"
                left, top, _, _ = detection.bbox
                estimated_width = max(96, len(label) * 9 + 16)
                x_offset = max(
                    24,
                    min(
                        int(left),
                        max(24, frame_width - estimated_width - 24),
                    ),
                )
                y_offset = max(reserved_top, int(top) - 24)
                dms_label_positions.append((label, x_offset, y_offset))

            # Keep model labels readable when several detections share a
            # similar top edge. The status/metric panel owns the reserved top.
            placed: list[tuple[int, int, int, int]] = []
            laid_out: list[tuple[str, int, int]] = []
            for label, x_offset, y_offset in sorted(
                dms_label_positions, key=lambda item: (item[2], item[1], item[0])
            ):
                estimated_width = max(96, len(label) * 9 + 16)
                y_offset = min(y_offset, max(reserved_top, frame_height - 30))
                while any(
                    x_offset < right
                    and x_offset + estimated_width > left
                    and y_offset < bottom
                    and y_offset + 22 > top
                    for left, top, right, bottom in placed
                ) and y_offset + 22 < frame_height - 8:
                    y_offset += 24
                placed.append(
                    (x_offset, y_offset, x_offset + estimated_width, y_offset + 22)
                )
                laid_out.append((label, x_offset, y_offset))
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_labels = 1 + len(laid_out)
            first = display_meta.text_params[0]
            first.display_text = status_label
            first.x_offset = 24
            first.y_offset = 24
            first.font_params.font_name = "Sans"
            first.font_params.font_size = 32
            if dms_result.alerts:
                first.font_params.font_color.set(1.0, 0.25, 0.10, 1.0)
            elif dms_result.status == "MONITORING":
                first.font_params.font_color.set(0.10, 1.0, 0.55, 1.0)
            else:
                first.font_params.font_color.set(1.0, 0.75, 0.10, 1.0)
            first.set_bg_clr = 1
            first.text_bg_clr.set(0.0, 0.0, 0.0, 0.70)
            for index, (label, x_offset, y_offset) in enumerate(laid_out, start=1):
                text_params = display_meta.text_params[index]
                text_params.display_text = label
                text_params.x_offset = x_offset
                text_params.y_offset = y_offset
                text_params.font_params.font_name = "Sans"
                text_params.font_params.font_size = 14
                text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
                text_params.set_bg_clr = 1
                text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.65)
            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
        if self.front_assistance_enabled:
            with self._analysis_lock:
                perception = self._front_perception
                banner_label = self.front_policy.banner_label
                recording = bool(self.front_policy.active_labels)
                geometry_diagnostics = self.front_policy.geometry_diagnostics
            if perception is not None:
                if perception.valid:
                    self._render_front_geometry(batch_meta, frame_meta, perception)
                self._render_front_hud(
                    batch_meta,
                    frame_meta,
                    perception,
                    banner_label=banner_label,
                    recording=recording,
                    geometry_diagnostics=geometry_diagnostics,
                )
        if not self.front_assistance_enabled:
            self._add_live_timestamp(batch_meta, frame_meta)

    def _front_geometry_segments(
        self,
        perception: FrontPerception,
    ) -> tuple[dict[str, Any], list[tuple[tuple[int, int], tuple[int, int], tuple[float, ...], int]]]:
        front_config = self.config.get("front_assistance", {}) or {}
        calibration = front_config.get("calibration", {}) or {}
        overlay_config = front_config.get("overlay", {}) or {}
        width = int(self.config["input"].get("width", 960))
        height = int(self.config["input"].get("height", 540))
        geometry = project_front_overlay(
            perception,
            calibration,
            width=width,
            height=height,
            lane_min_probability=float(
                overlay_config.get("lane_min_probability", 0.0)
            ),
            path_half_width_m=float(overlay_config.get("path_half_width_m", 0.9)),
            lead_min_probability=float(
                overlay_config.get("lead_min_probability", 0.5)
            ),
            road_edge_max_std_m=float(
                overlay_config.get("road_edge_max_std_m", 0.6)
            ),
        )
        segments: list[
            tuple[tuple[int, int], tuple[int, int], tuple[float, ...], int]
        ] = []

        def add_polyline(
            points: tuple[tuple[int, int], ...],
            color: tuple[float, ...],
            width_pixels: int,
        ) -> None:
            segments.extend(
                (left, right, color, width_pixels)
                for left, right in zip(points, points[1:], strict=False)
            )

        def add_filled_triangle(
            points: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
            color: tuple[float, ...],
        ) -> None:
            """Rasterize a filled triangle into bounded NvOSD line primitives."""
            min_y = min(point[1] for point in points)
            max_y = max(point[1] for point in points)
            for y in range(min_y, max_y + 1, 4):
                intersections: list[float] = []
                for start, end in zip(points, (*points[1:], points[0]), strict=True):
                    if start[1] == end[1] or not min(start[1], end[1]) <= y <= max(
                        start[1], end[1]
                    ):
                        continue
                    ratio = (y - start[1]) / (end[1] - start[1])
                    intersections.append(start[0] + ratio * (end[0] - start[0]))
                if len(intersections) >= 2:
                    segments.append(
                        (
                            (int(round(min(intersections))), y),
                            (int(round(max(intersections))), y),
                            color,
                            5,
                        )
                    )

        lane_colors = {
            0: (0.65, 0.65, 1.0),
            1: (0.05, 0.85, 1.0, 0.95),
            2: (0.15, 1.0, 0.35, 0.95),
            3: (0.7, 1.0, 0.7),
        }
        for lane in geometry.lanes:
            line_width = 5 if lane.confidence >= 0.5 else 3
            base_color = lane_colors.get(lane.lane_index, (0.8, 0.8, 0.8))
            color = (*base_color[:3], max(0.15, min(1.0, lane.confidence)))
            add_polyline(
                lane.points,
                color,
                line_width,
            )
        for edge in geometry.road_edges:
            color = (
                (1.0, 0.15, 0.7, edge.opacity)
                if edge.edge_index == 0
                else (0.7, 0.15, 1.0, edge.opacity)
            )
            add_polyline(edge.points, color, 4)
        add_polyline(geometry.path_left, (1.0, 0.45, 0.0, 0.95), 5)
        add_polyline(geometry.path_right, (1.0, 0.45, 0.0, 0.95), 5)
        add_polyline(geometry.path_center, (1.0, 0.95, 0.15, 0.95), 4)
        segments.extend(
            (left, right, (1.0, 0.65, 0.05, 0.60), 2)
            for left, right in zip(
                geometry.path_left,
                geometry.path_right,
                strict=False,
            )
        )
        for lead in geometry.leads:
            add_filled_triangle(
                lead.glow,
                (218.0 / 255.0, 202.0 / 255.0, 37.0 / 255.0, 1.0),
            )
            add_filled_triangle(
                lead.chevron,
                (201.0 / 255.0, 34.0 / 255.0, 49.0 / 255.0, lead.fill_alpha),
            )
        for horizon in geometry.horizons:
            marker_x, marker_y = horizon.point
            segments.append(
                (
                    (marker_x - 5, marker_y),
                    (marker_x + 5, marker_y),
                    (1.0, 1.0, 1.0, 0.8),
                    2,
                )
            )
        metrics = {
            **geometry.summary(),
            "rendered_segment_count": len(segments),
            "segments": [
                {
                    "x1": left[0],
                    "y1": left[1],
                    "x2": right[0],
                    "y2": right[1],
                    "color": [round(float(value), 4) for value in color],
                    "width": line_width,
                }
                for left, right, color, line_width in segments
            ],
            "horizons": [
                {"seconds": horizon.seconds, "x": horizon.point[0], "y": horizon.point[1]}
                for horizon in geometry.horizons
            ],
        }
        return metrics, segments

    def _render_front_geometry(
        self,
        batch_meta: Any,
        frame_meta: Any,
        perception: FrontPerception,
    ) -> None:
        """Render projected lane boundaries and a visible predicted path corridor."""
        metrics, segments = self._front_geometry_segments(perception)
        with self._analysis_lock:
            self._front_overlay_metrics = metrics
        for chunk in chunk_osd_items(segments):
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_lines = len(chunk)
            for index, (left, right, color, line_width) in enumerate(chunk):
                line = display_meta.line_params[index]
                line.x1, line.y1 = left
                line.x2, line.y2 = right
                line.line_width = line_width
                line.line_color.set(*color)
            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

    def _render_front_hud(
        self,
        batch_meta: Any,
        frame_meta: Any,
        perception: FrontPerception,
        *,
        banner_label: str | None,
        recording: bool,
        geometry_diagnostics: dict[str, Any],
    ) -> None:
        width = int(self.config["input"].get("width", 960))
        height = int(self.config["input"].get("height", 540))
        fps = float(self.config["input"].get("fps", 20.0))
        labels = list(
            build_front_hud(
                perception,
                banner_label=banner_label,
                fps=fps,
                recording=recording,
                width=width,
                height=height,
                geometry_diagnostics=geometry_diagnostics,
            )
        )
        for chunk in chunk_osd_items(labels):
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_labels = len(chunk)
            for index, label in enumerate(chunk):
                text_params = display_meta.text_params[index]
                text_params.display_text = label.text
                text_params.x_offset = label.x
                text_params.y_offset = label.y
                text_params.font_params.font_name = "Sans"
                text_params.font_params.font_size = label.font_size
                text_params.font_params.font_color.set(*label.color)
                text_params.set_bg_clr = 1
                text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.70)
            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

    def _on_output_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """Render backend-owned labels without applying an old ROI to a new frame."""
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        self.last_output_at = time.time()
        self.last_output_pts_ns = (
            None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts)
        )
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))
        if batch_meta is None:
            return Gst.PadProbeReturn.OK
        with self._analysis_lock:
            fire_smoke_updated_at = self._analysis_updated_at_by_function.get(
                "fire_smoke"
            )
            fire_smoke_age_seconds = (
                time.monotonic() - fire_smoke_updated_at
                if fire_smoke_updated_at is not None
                else self._analysis_result_max_age_seconds + 1.0
            )
            # Smoking is a temporal state owned by SmokingBehaviorEngine and
            # attached to the current track_id. Keep that confirmed state until
            # the engine clears it; a renderer TTL must not turn a confirmed
            # smoking track back into a plain person between slow analysis runs.
            detection_results = list(self._analysis_detections)
            dms_detections = list(self._analysis_dms_detections)
            dms_result = self._analysis_dms_result
            if fire_smoke_age_seconds <= self._analysis_result_max_age_seconds:
                fire_smoke_detections = list(self._analysis_fire_smoke)
            else:
                fire_smoke_detections = []
        frame_list = batch_meta.frame_meta_list
        while frame_list is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(frame_list.data)
                ntp_timestamp = int(getattr(frame_meta, "ntp_timestamp", 0) or 0)
                camera_latency_ms = _ntp_latency_ms(ntp_timestamp, self.last_output_at)
                if camera_latency_ms is not None:
                    source_timestamp = ntp_timestamp / 1_000_000_000
                    self.last_camera_source_timestamp = source_timestamp
                    self.camera_latency_samples += 1
                    if self.last_output_pts_ns is not None:
                        self._frame_timing_samples.append(
                            {
                                "rtp_timestamp": int(
                                    (self.last_output_pts_ns * 90_000 // 1_000_000_000)
                                    & 0xFFFFFFFF
                                ),
                                "capture_timestamp": source_timestamp,
                                "output_timestamp": self.last_output_at,
                                "output_pts_ns": self.last_output_pts_ns,
                            }
                        )
                    if self.last_camera_latency_ms is None:
                        self.last_camera_latency_ms = camera_latency_ms
                    else:
                        # Keep the displayed value stable while retaining the
                        # latest source timestamp for diagnostics.
                        self.last_camera_latency_ms = (
                            self.last_camera_latency_ms * 0.8
                            + camera_latency_ms * 0.2
                        )
                self._render_output_annotations(
                    batch_meta,
                    frame_meta,
                    detection_results,
                    fire_smoke_detections,
                    dms_detections,
                    dms_result,
                )
                frame_list = frame_list.next
            except StopIteration:
                break
            except Exception as exc:
                LOG.debug("live output annotation failed: %s", exc)
                break
        return Gst.PadProbeReturn.OK

    def _stop_analysis_worker(self) -> None:
        for executor in self._analysis_executors.values():
            executor.stop(timeout=5.0)
        self._analysis_executors.clear()

    def _write_runtime_status(self) -> bool:
        payload = {
            "camera": self.config["input"].get("camera", "unknown"),
            "run_id": self.run_id,
            "worker_epoch": self.evidence.worker_epoch,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": time.time(),
            "last_frame_at": self.last_frame_at,
            "last_output_at": self.last_output_at,
            "last_output_pts_ns": self.last_output_pts_ns,
            "camera_latency_ms": (
                round(self.last_camera_latency_ms, 1)
                if self.last_camera_latency_ms is not None
                else None
            ),
            "camera_source_timestamp": self.last_camera_source_timestamp,
            "camera_latency_source": (
                "rtcp_ntp" if self.camera_latency_samples else "unavailable"
            ),
            "camera_latency_samples": self.camera_latency_samples,
            "frame_count": self.frame_count,
            "input_decoder": getattr(self, "input_decoder", "unknown"),
            "input_mirror_horizontal": getattr(self, "input_mirror_horizontal", False),
            "output_encoder": getattr(self, "output_encoder", "unknown"),
            "output_resolution": getattr(self, "output_resolution", None),
            "output_rate_hz": getattr(self, "output_rate_hz", None),
            "opencv_threads": self.opencv_threads,
            "output_video_published": self.output_video_published,
            "config_generation": int(
                (self.config.get("runtime", {}) or {}).get("config_generation", 1)
            ),
            **self.execution_plan.status(),
            "analysis_queue_depth": sum(
                executor.queue_depth for executor in self._analysis_executors.values()
            ),
            "analysis_error": self._analysis_error,
            "analysis_flow": {
                "probe_count": self._analysis_probe_count,
                "due_count": self._analysis_due_count,
                "enqueued_count": self._analysis_enqueued_count,
                "processed_count": self._analysis_processed_count,
                "pre_conversion": self._analysis_admission_gate.status(),
                "last_enqueued_frame": self._analysis_last_enqueued_frame,
                "last_processed_frame": self._analysis_last_processed_frame,
                "result_age_seconds": {
                    function: round(time.monotonic() - updated_at, 3)
                    for function, updated_at in self._analysis_updated_at_by_function.items()
                },
                "result_max_age_seconds": self._analysis_result_max_age_seconds,
                "stale_drops": self._analysis_stale_drops,
                "out_of_order_drops": self._analysis_out_of_order_drops,
                "functions": {
                    function: executor.status()
                    for function, executor in self._analysis_executors.items()
                },
            },
            "analysis_decode": getattr(self.face_engine, "_last_decode_info", None),
            "analysis_debug": {
                "front_assistance": (
                    {
                        **self._front_perception.summary(),
                        "active_alerts": list(self.front_policy.active_labels),
                        "geometry_diagnostics": self.front_policy.geometry_diagnostics,
                        "overlay": dict(self._front_overlay_metrics),
                    }
                    if self._front_perception is not None
                    else {
                        "contract_version": 2,
                        "mode": "vision_only",
                        "readiness": "warming"
                        if self.front_assistance_enabled
                        else "disabled",
                        "active_alerts": [],
                        "overlay": dict(self._front_overlay_metrics),
                    }
                ),
                "smoking_person_count": getattr(self.smoking_behavior_engine, "last_person_count", 0),
                "smoking_scores": getattr(self.smoking_behavior_engine, "last_scores", {}),
                "smoking_roi_bboxes": getattr(
                    self.smoking_behavior_engine, "last_roi_bboxes", {}
                ),
                "smoking_invalid_crop_track_ids": getattr(
                    self.smoking_behavior_engine, "last_invalid_crop_track_ids", []
                ),
                "smoking_object_scores": getattr(
                    self.smoking_behavior_engine, "last_object_scores", {}
                ),
                "smoking_signal_sources": getattr(
                    self.smoking_behavior_engine, "last_signal_sources", {}
                ),
                "smoking_object_detections": [
                    {
                        "source": detection.source,
                        "label": detection.label,
                        "score": round(float(detection.score), 5),
                        "bbox": [round(float(value), 2) for value in detection.bbox],
                    }
                    for detection in getattr(
                        getattr(self.smoking_behavior_engine, "object_detector", None),
                        "last_detections",
                        [],
                    )
                ],
                "dms": {
                    "status": self._analysis_dms_result.status
                    if self._analysis_dms_result
                    else "DISABLED",
                    "alerts": list(self._analysis_dms_result.alerts)
                    if self._analysis_dms_result
                    else [],
                    "metrics": dict(self._analysis_dms_result.metrics)
                    if self._analysis_dms_result
                    else {},
                    "message": self._analysis_dms_result.message
                    if self._analysis_dms_result
                    else None,
                    "detections": [
                        {
                            "source": detection.source,
                            "original_class": detection.original_class,
                            "label": detection.label,
                            "score": round(float(detection.score), 5),
                            "bbox": [round(float(value), 2) for value in detection.bbox],
                            "person_track_id": detection.person_track_id,
                        }
                        for detection in self._analysis_dms_detections
                    ],
                    "raw_detections": [
                        {
                            "source": detection.source,
                            "original_class": detection.original_class,
                            "label": detection.label,
                            "score": round(float(detection.score), 5),
                            "bbox": [round(float(value), 2) for value in detection.bbox],
                            "person_track_id": detection.person_track_id,
                        }
                        for detection in (
                            self._analysis_dms_result.detections
                            if self._analysis_dms_result
                            else ()
                        )[:16]
                    ],
                    "events": self.dms_events.metrics(),
                    "active_event_ids": self.dms_events.active_event_ids,
                },
                "smoking_episodes": self.event_store.metrics(),
                "fire_smoke_raw_scores": getattr(self.fire_smoke_engine, "last_raw_scores", {}),
                "fire_smoke_regions": self.fire_smoke_events.metrics(),
                "fire_smoke_runtime": (
                    self.fire_smoke_engine.runtime_metrics()
                    if self.fire_smoke_engine is not None
                    else {"model": None, "providers": [], "inference_count": 0}
                ),
                "person_detector_count": self.last_person_count,
                "person_track_count": len(self._person_tracks),
                "person_candidate_count": sum(
                    1
                    for track in self._person_tracks.values()
                    if not track["confirmation"].confirmed
                ),
                "person_fire_smoke_overlap_count": self._last_person_fire_smoke_overlap_count,
                "person_fire_smoke_excluded_count": 0,
                "person_fire_smoke_excluded_last_frame": 0,
                "person_confirmed_count": sum(
                    1
                    for track in self._person_tracks.values()
                    if track["confirmation"].confirmed
                ),
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

    def _person_rois(
        self,
        frame_meta: Any,
        *,
        allow_cached: bool = False,
    ) -> list[tuple[int, float, float, float, float]]:
        persons: list[tuple[int, float, float, float, float]] = []
        for obj_meta in self._frame_objects(frame_meta):
            if str(obj_meta.obj_label) != self.config["person"]["label"]:
                continue
            track_id = int(obj_meta.object_id)
            if track_id in {0, 18446744073709551615}:
                continue
            if not self._person_track_confirmed(track_id):
                continue
            left = float(obj_meta.rect_params.left)
            top = float(obj_meta.rect_params.top)
            right = left + float(obj_meta.rect_params.width)
            bottom = top + float(obj_meta.rect_params.height)
            persons.append((track_id, left, top, right, bottom))
        if persons or not allow_cached:
            return persons
        with self._analysis_lock:
            if self._latest_person_updated_at is None:
                return []
            max_age = max(
                self._analysis_interval_seconds * 4.0,
                float(
                    (self.config.get("person", {}) or {}).get(
                        "roi_cache_max_age_seconds", 0.5
                    )
                ),
            )
            if time.monotonic() - self._latest_person_updated_at > max_age:
                return []
            return list(self._latest_person_rois)

    def _person_track_confirmed(self, track_id: int) -> bool:
        track = self._person_tracks.get(int(track_id))
        confirmation = track.get("confirmation") if track is not None else None
        return bool(confirmation is not None and confirmation.confirmed)

    def _person_box_overlaps_fresh_fire_smoke(self, box: np.ndarray) -> bool:
        """Correlation only; callers must never use overlap to suppress a person."""
        with self._analysis_lock:
            updated_at = self._analysis_updated_at_by_function.get("fire_smoke")
            if updated_at is None:
                return False
            age_seconds = time.monotonic() - updated_at
            if age_seconds > self._analysis_result_max_age_seconds:
                return False
            detections = list(self._analysis_fire_smoke)
        for detection in detections:
            if str(getattr(detection, "label", "")) not in {"fire", "smoke"}:
                continue
            other = np.asarray(detection.bbox, dtype=np.float32)
            if (
                intersection_over_candidate(box, other)
                >= 0.25
            ):
                return True
        return False

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
            for index, (_, box, _center) in enumerate(detections):
                overlap = iou(old_box, box)
                if opposite_frame_edge_transition(
                    old_box, box, frame_width, frame_height
                ):
                    continue
                distance = track_distance(box, estimate)
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
            track["confirmation"].observe(frame_number)
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
                "confirmation": PersonConfirmation(
                    required_hits=self._person_confirmation_hits,
                    window_frames=self._person_confirmation_window,
                ),
            }
            self._person_tracks[track_id]["confirmation"].observe(frame_number)
            obj.object_id = track_id
        self._last_person_fire_smoke_overlap_count = sum(
            int(self._person_box_overlaps_fresh_fire_smoke(box))
            for _obj, box, _center in detections
        )

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
            if label == "person" and not self._person_track_confirmed(int(track_id)):
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
            obj_meta.object_id = int(getattr(detection, "region_track_id", 0))
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

    def _attach_dms_objects(self, batch_meta: Any, frame_meta: Any, detections: list[Any]) -> None:
        """Render DMS model boxes on the DeepStream output surface."""
        colors = {
            "Seatbelt": (0.10, 0.73, 0.50, 1.0),
            "Smoking": (1.0, 0.25, 0.10, 1.0),
        }
        for detection in detections:
            left, top, right, bottom = detection.bbox
            obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
            obj_meta.class_id = 100
            obj_meta.object_id = 0
            obj_meta.confidence = float(detection.score)
            obj_meta.obj_label = f"dms_{detection.label}"
            obj_meta.rect_params.left = float(left)
            obj_meta.rect_params.top = float(top)
            obj_meta.rect_params.width = float(right - left)
            obj_meta.rect_params.height = float(bottom - top)
            obj_meta.rect_params.border_width = 3
            obj_meta.rect_params.border_color.set(
                *colors.get(detection.label, (1.0, 0.60, 0.05, 1.0))
            )
            obj_meta.rect_params.has_bg_color = 0
            # The visible DMS text is rendered in _render_output_annotations
            # so it can be clamped below the top panels and de-duplicated.
            obj_meta.text_params.display_text = ""
            obj_meta.text_params.set_bg_clr = 0
            pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)

    def _publish_metadata_frame(self, frame_meta: Any) -> None:
        """Publish lightweight overlay state from the non-blocking DeepStream probe."""
        self.frame_count += 1
        if self.frame_count % 100 == 0:
            LOG.info(
                "frames=%d smoking_bbox_count=%d fire_smoke_count=%d",
                self.frame_count,
                len(self._analysis_detections),
                len(self._analysis_fire_smoke),
            )
        now = time.monotonic()
        if now - self._metadata_write_at < self._metadata_write_interval_seconds:
            return
        self._metadata_write_at = now
        tracks = [] if self.dms_enabled else self._recognition_tracks(frame_meta)
        detection_results, fire_smoke_detections, transitions, _ = self._cached_analysis()
        with self._analysis_lock:
            face_tracks = list(self._face_tracks.values())
            dms_detections = list(self._analysis_dms_detections)
            dms_result = self._analysis_dms_result
            dms_transitions = list(self._analysis_transitions_by_function.get("dms", ()))
            fire_smoke_transitions = list(
                self._analysis_transitions_by_function.get("fire_smoke", ())
            )
            front_perception = self._front_perception
            front_transitions = list(self._front_transitions)
            front_overlay_metrics = dict(self._front_overlay_metrics)
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
                    "episode_sequence": detection.episode_sequence,
                    "confirmation_state": detection.confirmation_state,
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
                    "region_track_id": getattr(detection, "region_track_id", None),
                    "confirmation_state": getattr(detection, "confirmation_state", None),
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
        for detection in dms_detections:
            overlays.append(
                {
                    "kind": "dms",
                    "label": f"{detection.label} {float(detection.score) * 100:.0f}%",
                    "source": detection.source,
                    "original_class": detection.original_class,
                    "score": float(detection.score),
                    "left": detection.bbox[0],
                    "top": detection.bbox[1],
                    "right": detection.bbox[2],
                    "bottom": detection.bbox[3],
                }
            )
        payload = {
            "camera": self.config["input"].get("camera", "safety_camera"),
            "run_id": self.run_id,
            "width": int(self.config["input"].get("width", 1920)),
            "height": int(self.config["input"].get("height", 1080)),
            "frame_num": int(frame_meta.frame_num),
            "timestamp": time.time(),
            "frame_timing_samples": list(self._frame_timing_samples),
            "bbox_count": len(detection_results),
            "fire_smoke_count": len(fire_smoke_detections),
            "dms": {
                "status": dms_result.status if dms_result else "DISABLED",
                "alerts": list(dms_result.alerts) if dms_result else [],
                "metrics": dict(dms_result.metrics) if dms_result else {},
                "message": dms_result.message if dms_result else None,
                "detections": [
                    {
                        "source": detection.source,
                        "original_class": detection.original_class,
                        "label": detection.label,
                        "confidence": round(float(detection.score), 5),
                        "left": round(float(detection.bbox[0]), 2),
                        "top": round(float(detection.bbox[1]), 2),
                        "right": round(float(detection.bbox[2]), 2),
                        "bottom": round(float(detection.bbox[3]), 2),
                        "person_track_id": detection.person_track_id,
                    }
                    for detection in dms_detections
                ],
                "raw_detections": [
                    {
                        "source": detection.source,
                        "original_class": detection.original_class,
                        "label": detection.label,
                        "confidence": round(float(detection.score), 5),
                        "left": round(float(detection.bbox[0]), 2),
                        "top": round(float(detection.bbox[1]), 2),
                        "right": round(float(detection.bbox[2]), 2),
                        "bottom": round(float(detection.bbox[3]), 2),
                        "person_track_id": detection.person_track_id,
                    }
                    for detection in (dms_result.detections if dms_result else ())[:16]
                ],
                "events": self.dms_events.metrics(),
                "active_event_ids": self.dms_events.active_event_ids,
                "transitions": [
                    {
                        "operation": item.operation,
                        "event_id": item.event_id,
                        "label": item.label,
                        "sequence": item.alert_sequence,
                    }
                    for item in dms_transitions
                ],
            },
            "front_assistance": (
                {
                    **front_perception.summary(),
                    "active_alerts": list(self.front_policy.active_labels),
                    "geometry_diagnostics": self.front_policy.geometry_diagnostics,
                    "overlay": front_overlay_metrics,
                    "transitions": [
                        {
                            "operation": item.operation,
                            "event_id": item.event_id,
                            "label": item.label,
                            "frame_number": item.frame_number,
                        }
                        for item in front_transitions
                    ],
                }
                if front_perception is not None
                else {
                    "contract_version": 2,
                    "mode": "vision_only",
                    "readiness": "warming"
                    if self.front_assistance_enabled
                    else "disabled",
                    "active_alerts": [],
                    "overlay": front_overlay_metrics,
                    "transitions": [],
                }
            ),
            "event_id": self.event_store.active_event_id,
            "event_state": self.event_store.state.value if self.event_store.state else "IDLE",
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
                    "episode_sequence": detection.episode_sequence,
                    "confirmation_state": detection.confirmation_state,
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
                    "region_track_id": getattr(detection, "region_track_id", None),
                    "confirmation_state": getattr(detection, "confirmation_state", None),
                    "confidence": round(float(detection.score), 5),
                    "left": round(float(detection.bbox[0]), 2),
                    "top": round(float(detection.bbox[1]), 2),
                    "right": round(float(detection.bbox[2]), 2),
                    "bottom": round(float(detection.bbox[3]), 2),
                }
                for detection in fire_smoke_detections
            ],
            "fire_smoke_regions": self.fire_smoke_events.metrics(),
            "smoking_episodes": self.event_store.metrics(),
        }
        self._publish_live_metadata(payload)

    def _on_metadata_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        if not self.output_video_published:
            self.last_output_at = time.time()
            self.last_output_pts_ns = (
                None if buffer.pts == Gst.CLOCK_TIME_NONE else int(buffer.pts)
            )
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
        if self.dms_enabled:
            return
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        text_params = display_meta.text_params[0]
        timestamp = f"LIVE {dt.datetime.now().astimezone().strftime('%H:%M:%S.%f')[:-3]}"
        timestamp = f"{self.config['input'].get('camera', 'camera')} | {timestamp}"
        text_params.display_text = timestamp
        text_params.x_offset = 48
        text_params.y_offset = 50
        text_params.font_params.font_name = "Sans"
        text_params.font_params.font_size = 32
        text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
        text_params.set_bg_clr = 1
        text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.70)
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

    def _on_analysis_admission_buffer(
        self,
        _pad: Gst.Pad,
        _info: Gst.PadProbeInfo,
    ) -> Gst.PadProbeReturn:
        if self._analysis_admission_gate.accept(time.monotonic()):
            return Gst.PadProbeReturn.OK
        return Gst.PadProbeReturn.DROP

    def _on_output_admission_buffer(
        self,
        _pad: Gst.Pad,
        _info: Gst.PadProbeInfo,
    ) -> Gst.PadProbeReturn:
        gate = self._output_admission_gate
        if gate is None or gate.accept(time.monotonic()):
            return Gst.PadProbeReturn.OK
        return Gst.PadProbeReturn.DROP

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
                detection_results: list[Any] = []
                fire_smoke_detections: list[Any] = []
                if self.analysis_enabled:
                    now = time.monotonic()
                    due_functions = [
                        function
                        for function, executor in self._analysis_executors.items()
                        if executor.is_due(now)
                    ]
                    if due_functions:
                        self._analysis_due_count += len(due_functions)
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
                        persons = self._person_rois(frame_meta, allow_cached=True)
                        self._last_behavior_person_count = len(persons)
                        self._queue_analysis_sample(frame_meta, frame, persons, now)
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
                    self.last_event_transition = None
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
                            "episode_sequence": detection.episode_sequence,
                            "confirmation_state": detection.confirmation_state,
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
                            "region_track_id": getattr(detection, "region_track_id", None),
                            "confirmation_state": getattr(detection, "confirmation_state", None),
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
                    "event_state": self.event_store.state.value if self.event_store.state else "IDLE",
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
                            "episode_sequence": detection.episode_sequence,
                            "confirmation_state": detection.confirmation_state,
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
                            "region_track_id": getattr(detection, "region_track_id", None),
                            "confirmation_state": getattr(detection, "confirmation_state", None),
                            "confidence": round(float(detection.score), 5),
                            "left": round(float(detection.bbox[0]), 2),
                            "top": round(float(detection.bbox[1]), 2),
                            "right": round(float(detection.bbox[2]), 2),
                            "bottom": round(float(detection.bbox[3]), 2),
                        }
                        for detection in fire_smoke_detections
                    ],
                    "fire_smoke_regions": self.fire_smoke_events.metrics(),
                    "smoking_episodes": self.event_store.metrics(),
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
        sync_group = str(input_cfg.get("mock_sync_group", "")).strip()
        sync_period = float(input_cfg.get("mock_sync_period_seconds", 0.0))
        sync_epoch = float(input_cfg.get("mock_sync_epoch_seconds", 0.0))
        synchronized = bool(sync_group) and sync_period > 0.0
        if synchronized:
            LOG.info(
                "waiting for externally owned mock timeline: group=%s period=%.3f epoch=%.3f",
                sync_group,
                sync_period,
                sync_epoch,
            )
            wait_for_rtsp_video(str(input_cfg["rtsp_url"]))
            LOG.info("external synchronized mock input ready: %s", mock_video)
            return
        if shutil.which("ffmpeg"):
            publisher_stderr: int | None = subprocess.DEVNULL
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
                    "20" if self.front_assistance_enabled else "15",
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
        else:
            publisher_stderr = None
            command = [
                sys.executable,
                "-m",
                "adapters.media.gstreamer_mock_publisher",
                "--input",
                str(mock_video),
                "--output",
                str(input_cfg["rtsp_url"]),
                "--fps",
                "20" if self.front_assistance_enabled else "15",
            ]
            if bool(input_cfg.get("mock_loop", True)):
                command.append("--loop")
            LOG.warning(
                "ffmpeg is unavailable; using the bounded GStreamer mock publisher"
            )
        self.mock_publisher = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=publisher_stderr,
        )
        # FFmpeg can remain alive for tens of seconds before its first encoded
        # frame reaches MediaMTX while all camera models initialize together.
        # Do not start rtspsrc until the path contains a readable video stream.
        wait_for_rtsp_video(
            str(input_cfg["rtsp_url"]),
            publisher_alive=lambda: (
                self.mock_publisher is not None
                and self.mock_publisher.poll() is None
            ),
        )
        LOG.info("mock input started: %s", mock_video)

    def _check_mock_publisher(self) -> bool:
        input_cfg = self.config["input"]
        if self.mock_publisher is None:
            return GLib.SOURCE_REMOVE
        if self.mock_publisher.poll() is not None:
            reason = (
                "exited unexpectedly"
                if bool(input_cfg.get("mock_loop", True))
                else "reached EOF"
            )
            LOG.warning("mock input %s; stopping worker for supervised recovery", reason)
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
            if str(self.config["input"].get("mode", "rtsp")) == "mock":
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
            try:
                self.event_store.close()
            except Exception:
                LOG.exception("smoking event close failed; continuing shutdown")
            try:
                self.dms_events.close()
            except Exception:
                LOG.exception("DMS event close failed; continuing shutdown")
            try:
                self.fire_smoke_events.close()
            except Exception:
                LOG.exception("fire/smoke event close failed; continuing shutdown")
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


def _run_e2e_mock_worker(config_path: Path, camera_id: str, run_id: str, worker_epoch: str) -> int:
    """Run a model-free E2E worker and publish the real fixture to MediaMTX."""
    config = load_config(config_path, camera_id)
    input_config = config.get("input", {}) or {}
    output_config = config.get("output", {}) or {}
    mock_video = Path(str(input_config.get("mock_video", "")))
    output_url = str(output_config.get("rtsp_url", ""))
    if not mock_video.is_file():
        LOG.error("E2E mock fixture does not exist: %s", mock_video)
        return 1
    if not output_url:
        LOG.error("E2E mock output URL is missing for camera=%s", camera_id)
        return 1

    status_dir = Path(str((config.get("runtime", {}) or {}).get("status_directory", "/opt/ls-vision/data/status")))
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path = status_dir / f"{camera_id}.json"
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)

    publisher = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "adapters.media.gstreamer_mock_publisher",
            "--input",
            str(mock_video),
            "--output",
            output_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    parsed_output = urlparse(output_url)
    hls_url = f"http://{parsed_output.hostname}:8888{parsed_output.path.rstrip('/')}/index.m3u8"
    hls_ready = False
    deadline = time.monotonic() + 45.0
    frame_count = 0
    started_at = time.time()
    try:
        while not stopping and not hls_ready:
            if publisher.poll() is not None:
                LOG.error("E2E GStreamer publisher exited before HLS became ready: %s", output_url)
                return 1
            try:
                with urlopen(hls_url, timeout=2.0) as response:  # noqa: S310 - internal MediaMTX URL
                    hls_ready = response.status == 200 and b"#EXTM3U" in response.read(256)
            except OSError:
                pass
            if not hls_ready and time.monotonic() >= deadline:
                LOG.error("E2E HLS output did not become ready: %s", hls_url)
                return 1
            time.sleep(0.2)

        while not stopping and publisher.poll() is None:
            now = time.time()
            frame_count += 1
            payload = {
                "camera": camera_id,
                "run_id": run_id,
                "worker_epoch": worker_epoch,
                "ready": True,
                "frame_count": frame_count,
                "last_frame_at": now,
                "last_output_at": now,
                "last_output_pts_ns": frame_count * 200_000_000,
                "analysis_queue_depth": 0,
                "analysis_error": None,
                "uptime_seconds": round(now - started_at, 3),
                "input_mode": "mock",
                "mock_adapter": True,
            }
            temporary = status_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(status_path)
            time.sleep(0.2)
    finally:
        if publisher.poll() is None:
            publisher.terminate()
            try:
                publisher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                publisher.kill()
                publisher.wait(timeout=5)
        status_path.unlink(missing_ok=True)
    return 0

def run_camera_process(
    config_path: Path,
    camera_id: str | None,
    run_id: str,
    worker_epoch: str,
    duration: int | None = None,
    config_generation: int = 1,
    expected_plan_hash: str | None = None,
) -> int:
    """Run one configured camera process from the application entrypoint."""
    raw_config = load_raw_config(config_path)
    if str(raw_config.get("profile", "")).lower() == "e2e":
        return _run_e2e_mock_worker(
            config_path,
            camera_id or "camera",
            run_id,
            worker_epoch,
        )
    Gst.init(None)
    config = load_config(config_path, camera_id)
    config.setdefault("runtime", {})["worker_epoch"] = worker_epoch
    config["runtime"]["config_generation"] = config_generation
    plan = compile_camera_plan(config)
    if expected_plan_hash is not None and plan.plan_hash != expected_plan_hash:
        raise RuntimeError(
            f"camera {camera_id} config changed during worker start: "
            f"expected={expected_plan_hash} actual={plan.plan_hash}"
        )
    pipeline = DeepStreamCameraRuntime(config, config_path, run_id)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: pipeline.loop.quit())
    pipeline.run(duration)
    return 0
