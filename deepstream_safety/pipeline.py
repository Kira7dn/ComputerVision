#!/usr/bin/env python3
"""Standalone DeepStream Safety pipeline.

The pipeline owns only one video path:
RTSP input -> person detector -> Python person tracker -> cigarette detector
-> Python tensor decode -> NVOSD -> RTSP output.
It does not import Frigate or read any Frigate configuration.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

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

import gi
import numpy as np
import pyds
import yaml
import zmq

from events import SafetyEventStore
from face_engine import FaceRecognitionEngine
from recognition import RecognitionCore, TrackKey

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402


LOG = logging.getLogger("deepstream-safety")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


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
    def __init__(self, config: dict[str, Any], config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.Pipeline.new("deepstream-safety")
        if self.pipeline is None:
            raise RuntimeError("Unable to create GStreamer pipeline")
        self.depay: Gst.Element | None = None
        self.person_infer: Gst.Element | None = None
        self.infer: Gst.Element | None = None
        self.frame_probe_id: int | None = None
        self.started_at = time.monotonic()
        self.frame_count = 0
        self.person_frame_count = 0
        self.last_person_count = 0
        self.last_bbox_count = 0
        self.tensor_logged = False
        self.person_tensor_logged = False
        self.person_score_logged = False
        self.event_store = SafetyEventStore(config)
        self.mock_publisher: subprocess.Popen | None = None
        self.recognition = RecognitionCore(config)
        self.face_engine = FaceRecognitionEngine(config)
        self.recognition_last_frame: dict[TrackKey, int] = {}
        self.last_event_transition: str | None = None
        snapshot_cfg = config.get("snapshots", {})
        self.snapshot_enabled = bool(snapshot_cfg.get("enabled", False))
        self.snapshot_dir = Path(snapshot_cfg.get("directory", "/tmp/deepstream-safety/snapshots"))
        self.snapshot_recognized_dir = self.snapshot_dir / "recognized"
        self.snapshot_unrecognized_dir = self.snapshot_dir / "unrecognized"
        self.snapshot_count = 0
        self._recognized_track_ids: set[int] = set()
        self._recognized_track_names: dict[int, str] = {}
        self._recognized_snapshot_names: set[str] = set()
        self._unknown_snapshot_frames: dict[Path, set[int]] = {}
        self._pending_output_evidence: list[dict[str, Any]] = []
        self._event_snapshot_saved_ids: set[str] = set()
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
        self.last_tensor_error: str | None = None
        if self.snapshot_enabled:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            self.snapshot_recognized_dir.mkdir(parents=True, exist_ok=True)
            self.snapshot_unrecognized_dir.mkdir(parents=True, exist_ok=True)
        self.socket = zmq.Context.instance().socket(zmq.PUB)
        self.socket.bind(config["metadata"]["zmq_pub_url"])
        self.infer_config = self._write_infer_config()
        self.person_infer_config = self._write_person_infer_config()
        self._build()

    def _write_infer_config(self) -> str:
        model = self.config["model"]
        model_path = os.path.abspath(model["onnx_path"])
        engine_path = "/opt/camera-deepstream/models/safety-smoking.engine"
        model_source = (
            f"model-engine-file={engine_path}"
            if Path(engine_path).is_file()
            else f"onnx-file={model_path}\nmodel-engine-file={engine_path}"
        )
        content = f"""[property]
gpu-id=0
{model_source}
labelfile-path=/tmp/deepstream-safety-labels.txt
batch-size=1
network-mode=2
network-type=100
model-color-format=1
net-scale-factor=0.00392156862745098
num-detected-classes=1
infer-dims=3;{int(model['input_width'])};{int(model['input_height'])}
interval=0
gie-unique-id=1
process-mode=1
output-tensor-meta=1
maintain-aspect-ratio=0
"""
        labels = f"{model['label']}\n"
        labels_path = "/tmp/deepstream-safety-labels.txt"
        Path(labels_path).write_text(labels, encoding="utf-8")
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix="deepstream-safety-infer-", suffix=".txt", delete=False
        )
        with handle:
            handle.write(content)
        LOG.info("nvinfer config: %s", handle.name)
        return handle.name

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
        source.set_property("latency", int(input_cfg["latency_ms"]))
        source.set_property("protocols", 4)
        self.depay = make_element("rtph264depay", "rtp-h264-depay")
        parser = make_element("h264parse", "input-h264-parse")
        decoder = make_element("nvv4l2decoder", "input-decoder")
        mux = make_element("nvstreammux", "stream-muxer")
        mux.set_property("batch-size", 1)
        mux.set_property("width", int(input_cfg["width"]))
        mux.set_property("height", int(input_cfg["height"]))
        mux.set_property("live-source", 1)
        mux.set_property("batched-push-timeout", 40000)

        self.person_infer = make_element("nvinfer", "person-inference")
        self.person_infer.set_property("config-file-path", self.person_infer_config)
        self.infer = make_element("nvinfer", "smoking-inference")
        self.infer.set_property("config-file-path", self.infer_config)
        face_cpu_convert = make_element("nvvideoconvert", "face-cpu-convert")
        face_cpu_caps = make_element("capsfilter", "face-cpu-caps")
        face_cpu_caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGRx"))
        convert_before_osd = make_element("nvvideoconvert", "convert-before-osd")
        face_rgba_caps = make_element("capsfilter", "face-rgba-caps")
        face_rgba_caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM),format=RGBA"))
        osd = make_element("nvdsosd", "bbox-osd")
        osd.set_property("display-bbox", True)
        osd.set_property("display-text", True)
        convert_after_osd = make_element("nvvideoconvert", "convert-after-osd")
        output_i420_caps = make_element("capsfilter", "output-i420-caps")
        output_i420_caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420"))
        output_tee = make_element("tee", "output-tee")
        output_queue = make_element("queue", "output-queue")
        encoder = make_element("x264enc", "output-encoder")
        encoder.set_property("bitrate", 4_000)
        encoder.set_property("speed-preset", "ultrafast")
        encoder.set_property("tune", "zerolatency")
        encoder.set_property("key-int-max", 30)
        output_parser = make_element("h264parse", "output-h264-parse")
        output_parser.set_property("config-interval", 1)
        sink = make_element("rtspclientsink", "rtsp-output")
        sink.set_property("location", output_cfg["rtsp_url"])
        snapshot_queue = make_element("queue", "snapshot-queue")
        snapshot_convert = make_element("nvvideoconvert", "snapshot-convert")
        snapshot_caps = make_element("capsfilter", "snapshot-caps")
        snapshot_caps.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420"))
        snapshot_encoder = make_element("jpegenc", "snapshot-jpeg-encoder")
        snapshot_sink = make_element("appsink", "snapshot-sink")
        snapshot_sink.set_property("emit-signals", True)
        snapshot_sink.set_property("sync", False)
        snapshot_sink.set_property("max-buffers", 1)
        snapshot_sink.set_property("drop", True)
        snapshot_sink.set_property("caps", Gst.Caps.from_string("image/jpeg"))
        snapshot_sink.connect("new-sample", self._on_snapshot_sample)

        elements = [
            source,
            self.depay,
            parser,
            decoder,
            mux,
            self.person_infer,
            self.infer,
            face_cpu_convert,
            face_cpu_caps,
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
            snapshot_queue,
            snapshot_convert,
            snapshot_caps,
            snapshot_encoder,
            snapshot_sink,
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
        if not self.person_infer.link(self.infer):
            raise RuntimeError("Unable to link person detector to cigarette nvinfer")
        if not self.infer.link(face_cpu_convert):
            raise RuntimeError("Unable to link smoking nvinfer to face converter")
        infer_src = self.infer.get_static_pad("src")
        if infer_src is None:
            raise RuntimeError("Smoking nvinfer has no src pad")
        infer_src.add_probe(Gst.PadProbeType.BUFFER, self._on_metadata_buffer)
        if not face_cpu_convert.link(face_cpu_caps):
            raise RuntimeError("Unable to link face converter to CPU caps")
        if not face_cpu_caps.link(convert_before_osd) or not convert_before_osd.link(face_rgba_caps):
            raise RuntimeError("Unable to link CPU face frame to OSD converter")
        if not face_rgba_caps.link(osd) or not osd.link(convert_after_osd):
            raise RuntimeError("Unable to link OSD branch")
        if not convert_after_osd.link(output_i420_caps) or not output_i420_caps.link(output_tee):
            raise RuntimeError("Unable to link output tee")
        if not output_tee.link(output_queue) or not output_tee.link(snapshot_queue):
            raise RuntimeError("Unable to link output branches")
        if not output_queue.link(encoder) or not encoder.link(output_parser):
            raise RuntimeError("Unable to link output encoder")
        if not output_parser.link(sink):
            raise RuntimeError("Unable to link RTSP output")
        if not snapshot_queue.link(snapshot_convert) or not snapshot_convert.link(snapshot_caps):
            raise RuntimeError("Unable to link snapshot conversion")
        if not snapshot_caps.link(snapshot_encoder) or not snapshot_encoder.link(snapshot_sink):
            raise RuntimeError("Unable to link snapshot sink")
        person_src = self.person_infer.get_static_pad("src")
        if person_src is None:
            raise RuntimeError("Person nvinfer has no src pad")
        person_src.add_probe(Gst.PadProbeType.BUFFER, self._on_person_buffer)
        face_src = face_cpu_caps.get_static_pad("src")
        if face_src is None:
            raise RuntimeError("CPU face probe has no src pad")
        self.frame_probe_id = face_src.add_probe(
            Gst.PadProbeType.BUFFER, self._on_infer_buffer
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

    def _decode_boxes(self, tensor_meta: Any, frame_width: int, frame_height: int) -> list[np.ndarray]:
        model = self.config["model"]
        threshold = float(model["confidence"])
        input_width = float(model["input_width"])
        input_height = float(model["input_height"])
        for index in range(int(tensor_meta.num_output_layers)):
            layer = pyds.get_nvds_LayerInfo(tensor_meta, index)
            values, dims = self._layer_array(layer)
            if not self.tensor_logged:
                channel_major = values.reshape(dims).T
                candidate_major = values.reshape((-1, dims[0]))
                channel_scores = channel_major[:, 4]
                candidate_scores = candidate_major[:, 4]
                LOG.info("tensor dims=%s elements=%d min=%.6f max=%.6f first=%s", dims, values.size,
                         float(values.min()), float(values.max()), np.round(values[:10], 4).tolist())
                LOG.info("tensor layouts channel_major score_max=%.6f score_count=%.0f candidate_major score_max=%.6f score_count=%.0f",
                         float(channel_scores.max()), float((channel_scores >= 0.25).sum()),
                         float(candidate_scores.max()), float((candidate_scores >= 0.25).sum()))
                self.tensor_logged = True
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
            boxes: list[np.ndarray] = []
            scale_x = frame_width / input_width
            scale_y = frame_height / input_height
            for row in matrix:
                score = float(row[4]) if matrix.shape[1] == 5 else float(np.max(row[4:]))
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
                        obj_meta.rect_params.border_width = 2
                        obj_meta.rect_params.border_color.set(0.1, 1.0, 0.1, 1.0)
                        obj_meta.rect_params.has_bg_color = 0
                        obj_meta.text_params.display_text = f"person {float(box[4]):.2f}"
                        obj_meta.text_params.font_params.font_name = "Sans"
                        obj_meta.text_params.font_params.font_size = 14
                        obj_meta.text_params.set_bg_clr = 1
                        obj_meta.text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.65)
                        pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)
                self.last_person_count = person_count
                self.person_frame_count += 1
                if self.person_frame_count % 100 == 0:
                    LOG.info("person_frames=%d person_count=%d", self.person_frame_count, person_count)
                frame_list = frame_list.next
            except StopIteration:
                break
        return Gst.PadProbeReturn.OK

    def _smoking_boxes_inside_persons(self, frame_meta: Any, boxes: list[np.ndarray]) -> list[np.ndarray]:
        persons = []
        for obj_meta in self._frame_objects(frame_meta):
            if str(obj_meta.obj_label) != self.config["person"]["label"]:
                continue
            rect = obj_meta.rect_params
            left = float(rect.left)
            top = float(rect.top)
            right = left + float(rect.width)
            bottom = top + float(rect.height)
            expand_x = (right - left) * 0.20
            expand_y = (bottom - top) * 0.20
            persons.append((left - expand_x, top - expand_y, right + expand_x, bottom + expand_y))
        if not persons:
            return []
        return [
            box for box in boxes
            if any(
                left <= (float(box[0]) + float(box[2])) / 2.0 <= right
                and top <= (float(box[1]) + float(box[3])) / 2.0 <= bottom
                for left, top, right, bottom in persons
            )
        ]

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
            delta = box - previous_box
            previous_velocity = track.get(
                "velocity", np.zeros(4, dtype=np.float32)
            )
            track.update(
                box=box,
                center=center,
                velocity=previous_velocity * 0.7 + delta * 0.3,
                disappeared=0,
                last_frame=frame_number,
                frames_seen=int(track.get("frames_seen", 0)) + 1,
            )
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

    def _discard_unknown_evidence(self, track_id: int) -> None:
        for path, track_ids in list(self._unknown_snapshot_frames.items()):
            if track_id not in track_ids:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._unknown_snapshot_frames.pop(path, None)

    def _queue_recognition_evidence(
        self,
        frame_tracks: list[dict[str, Any]],
        face_tracks: dict[int, dict[str, Any]],
        frame_number: int,
        source_pts: int,
    ) -> None:
        """Queue evidence until the corresponding post-OSD output frame arrives."""
        if not self.snapshot_enabled:
            return
        recognized_names: set[str] = set()
        unknown_track_ids: set[int] = set()
        recognized_present = False
        for track in frame_tracks:
            if track.get("label") != "person":
                continue
            track_id = int(track["track_id"])
            result = face_tracks.get(track_id)
            if not result:
                continue
            name = str(result.get("name") or "unknown")
            if name != "unknown" or track_id in self._recognized_track_ids:
                recognized_present = True
            if not result.get("attempted"):
                continue
            if name != "unknown":
                self._recognized_track_ids.add(track_id)
                self._recognized_track_names[track_id] = name
                self._discard_unknown_evidence(track_id)
                if name not in self._recognized_snapshot_names:
                    recognized_names.add(name)
            elif track_id not in self._recognized_track_ids:
                unknown_track_ids.add(track_id)
        if recognized_present:
            unknown_track_ids.clear()
        if recognized_names or unknown_track_ids:
            self._pending_output_evidence.append(
                {
                    "source_pts": source_pts,
                    "frame": frame_number,
                    "recognized_names": recognized_names,
                    "unknown_track_ids": unknown_track_ids,
                }
            )

    def _attach_objects(self, batch_meta: Any, frame_meta: Any, boxes: list[np.ndarray]) -> None:
        label = self.config["model"]["label"]
        for box in boxes:
            obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
            obj_meta.class_id = 0
            obj_meta.confidence = float(box[4])
            obj_meta.obj_label = label
            obj_meta.rect_params.left = float(box[0])
            obj_meta.rect_params.top = float(box[1])
            obj_meta.rect_params.width = float(box[2] - box[0])
            obj_meta.rect_params.height = float(box[3] - box[1])
            obj_meta.rect_params.border_width = 3
            obj_meta.rect_params.border_color.set(1.0, 0.1, 0.1, 1.0)
            obj_meta.rect_params.has_bg_color = 0
            obj_meta.text_params.display_text = f"{label} {float(box[4]):.2f}"
            obj_meta.text_params.font_params.font_name = "Sans"
            obj_meta.text_params.font_params.font_size = 14
            obj_meta.text_params.set_bg_clr = 1
            obj_meta.text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.65)
            pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)

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
                frame_list = frame_list.next
            except StopIteration:
                break
        return Gst.PadProbeReturn.OK

    def _on_infer_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
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
                tensor_meta = self._get_tensor_meta(frame_meta, batch_meta, 1)
                boxes = self._decode_boxes(
                    tensor_meta,
                    int(self.config["input"]["width"]),
                    int(self.config["input"]["height"]),
                ) if tensor_meta is not None else []
                boxes = self._smoking_boxes_inside_persons(frame_meta, boxes)
                self._attach_objects(batch_meta, frame_meta, boxes)
                tracks = self._recognition_tracks(frame_meta)
                self.last_bbox_count = len(boxes)
                transition = self.event_store.observe(
                    frame_num=int(frame_meta.frame_num),
                    timestamp=time.time(),
                    detections=[
                        (float(box[4]), (float(box[0]), float(box[1]), float(box[2]), float(box[3])))
                        for box in boxes
                    ],
                )
                self.last_event_transition = transition.operation if transition else None
                face_tracks = self.face_engine.process(buffer, frame_meta, int(frame_meta.frame_num))
                labels: list[tuple[str, int, int]] = []
                for obj_meta in self._frame_objects(frame_meta):
                    if str(obj_meta.obj_label) != self.config["person"]["label"]:
                        continue
                    name, score = self.face_engine.current_label(
                        int(obj_meta.object_id), int(frame_meta.frame_num)
                    )
                    label = f"person {name} {score:.2f}"
                    obj_meta.text_params.display_text = label
                    labels.append(
                        (
                            label,
                            int(obj_meta.rect_params.left),
                            max(0, int(obj_meta.rect_params.top) - 28),
                        )
                    )
                if labels:
                    display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
                    display_meta.num_labels = len(labels)
                    for index, (label, left, top) in enumerate(labels):
                        text_params = display_meta.text_params[index]
                        text_params.display_text = label
                        text_params.x_offset = left
                        text_params.y_offset = top
                        text_params.font_params.font_name = "Sans"
                        text_params.font_params.font_size = 18
                        text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
                        text_params.set_bg_clr = 1
                        text_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.75)
                    pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
                self._queue_recognition_evidence(
                    tracks,
                    face_tracks,
                    int(frame_meta.frame_num),
                    int(getattr(buffer, "pts", -1)),
                )
                payload = {
                    "camera": "safety_mock",
                    "frame_num": int(frame_meta.frame_num),
                    "timestamp": time.time(),
                    "bbox_count": len(boxes),
                    "recognition_enabled": self.recognition.enabled,
                    "tracks": tracks,
                    "face_tracks": list(face_tracks.values()),
                    "boxes": [
                        {"label": self.config["model"]["label"], "confidence": round(float(box[4]), 5),
                         "left": round(float(box[0]), 2), "top": round(float(box[1]), 2),
                         "right": round(float(box[2]), 2), "bottom": round(float(box[3]), 2)}
                        for box in boxes
                    ],
                }
                self.socket.send_json(payload, flags=zmq.NOBLOCK)
                self.frame_count += 1
                if self.frame_count % 100 == 0:
                    LOG.info("frames=%d bbox_count=%d", self.frame_count, self.last_bbox_count)
                frame_list = frame_list.next
            except StopIteration:
                break
            except Exception as exc:  # keep video flowing if one malformed tensor arrives
                message = str(exc)
                if message != self.last_tensor_error:
                    LOG.exception("Tensor decode failed: %s", message)
                    self.last_tensor_error = message
                break
        return Gst.PadProbeReturn.OK

    def _on_snapshot_sample(self, sink: Gst.Element) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        if buffer is None:
            return Gst.FlowReturn.OK
        success, mapped = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK
        try:
            content = bytes(mapped.data)
            pts = int(getattr(buffer, "pts", -1))
            self.face_engine.update_jpeg(content, pts)
            ready: list[dict[str, Any]] = []
            waiting: list[dict[str, Any]] = []
            for request in self._pending_output_evidence:
                source_pts = int(request["source_pts"])
                if (source_pts < 0 and pts >= 0) or (pts >= 0 and pts >= source_pts):
                    ready.append(request)
                else:
                    waiting.append(request)
            self._pending_output_evidence = waiting
            for request in ready:
                event_id = self.event_store.active_event_id
                if event_id and event_id not in self._event_snapshot_saved_ids:
                    self.event_store.save_snapshot(content, time.monotonic())
                    self._event_snapshot_saved_ids.add(event_id)
                for name in request["recognized_names"]:
                    safe_name = "".join(
                        char if char.isalnum() or char in "-_" else "_" for char in name
                    )
                    path = self.snapshot_recognized_dir / (
                        f"{safe_name}-frame-{int(request['frame']):08d}.jpg"
                    )
                    if name not in self._recognized_snapshot_names:
                        path.write_bytes(content)
                        self._recognized_snapshot_names.add(name)
                        self.snapshot_count += 1
                if request["unknown_track_ids"]:
                    path = self.snapshot_unrecognized_dir / (
                        f"frame-{int(request['frame']):08d}-"
                        f"{self.snapshot_count:06d}.jpg"
                    )
                    path.write_bytes(content)
                    self._unknown_snapshot_frames[path] = set(
                        request["unknown_track_ids"]
                    )
                    self.snapshot_count += 1
        finally:
            buffer.unmap(mapped)
        return Gst.FlowReturn.OK

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
            self.face_engine.close()
            self.pipeline.set_state(Gst.State.NULL)
            self.event_store.close()
            self.socket.close(0)
            LOG.info("pipeline stopped; frames=%d snapshots=%d uptime=%.1fs", self.frame_count, self.snapshot_count,
                     time.monotonic() - self.started_at)

    def _stop_after_duration(self) -> bool:
        self.loop.quit()
        return GLib.SOURCE_REMOVE


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--duration", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gst.init(None)
    pipeline = SafetyPipeline(load_config(args.config), args.config)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: pipeline.loop.quit())
    pipeline.run(args.duration)


if __name__ == "__main__":
    main()
