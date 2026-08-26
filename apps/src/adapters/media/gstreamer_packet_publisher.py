"""Publish an all-intra H.264 MP4 fixture without decoding or re-encoding it."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path

import gi

from domain.mock_timeline import frame_index_for_timestamp

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def make_element(factory: str, name: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer packet publisher element is unavailable: {factory}")
    return element


def _write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def run(
    input_path: Path,
    output_url: str,
    *,
    repeat: bool = False,
    fps: int = 20,
    sync_period_seconds: float = 0.0,
    sync_epoch_seconds: float = 0.0,
    camera_id: str | None = None,
    sync_group: str | None = None,
    status_path: Path | None = None,
) -> int:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if fps <= 0:
        raise ValueError("fps must be positive")
    synchronized = sync_period_seconds > 0.0
    if not synchronized:
        raise ValueError("packet-copy publisher requires a synchronized timeline")
    frame_count = round(sync_period_seconds * fps)
    if frame_count <= 0:
        raise ValueError("synchronized fixture must contain at least one frame")

    Gst.init(None)
    reader = Gst.Pipeline.new("packet-reader")
    file_source = make_element("filesrc", "file-source")
    demux = make_element("qtdemux", "demux")
    reader_queue = make_element("queue", "reader-queue")
    reader_parser = make_element("h264parse", "reader-parser")
    reader_caps = make_element("capsfilter", "reader-caps")
    app_sink = make_element("appsink", "packet-sink")
    file_source.set_property("location", str(input_path))
    reader_caps.set_property(
        "caps",
        Gst.Caps.from_string("video/x-h264,stream-format=byte-stream,alignment=au"),
    )
    app_sink.set_property("sync", False)
    app_sink.set_property("max-buffers", 2)
    app_sink.set_property("drop", False)
    for element in (file_source, demux, reader_queue, reader_parser, reader_caps, app_sink):
        reader.add(element)
    if not file_source.link(demux) or any(
        not left.link(right)
        for left, right in zip(
            (reader_queue, reader_parser, reader_caps),
            (reader_parser, reader_caps, app_sink),
            strict=True,
        )
    ):
        raise RuntimeError("Unable to link packet reader pipeline")

    def link_video_pad(_demux: Gst.Element, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        structure = caps.get_structure(0) if caps and caps.get_size() else None
        if structure is None or not structure.get_name().startswith("video/"):
            return
        sink_pad = reader_queue.get_static_pad("sink")
        if sink_pad is not None and not sink_pad.is_linked():
            result = pad.link(sink_pad)
            if result != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"Unable to link MP4 video pad: {result}")

    demux.connect("pad-added", link_video_pad)

    publisher = Gst.Pipeline.new("packet-publisher")
    app_source = make_element("appsrc", "packet-source")
    output_parser = make_element("h264parse", "output-parser")
    rtsp_sink = make_element("rtspclientsink", "rtsp-sink")
    app_source.set_property("is-live", True)
    app_source.set_property("block", True)
    app_source.set_property("format", Gst.Format.TIME)
    app_source.set_property(
        "caps",
        Gst.Caps.from_string("video/x-h264,stream-format=byte-stream,alignment=au"),
    )
    output_parser.set_property("config-interval", -1)
    rtsp_sink.set_property("location", output_url)
    rtsp_sink.set_property("protocols", "tcp")
    for element in (app_source, output_parser, rtsp_sink):
        publisher.add(element)
    if not app_source.link(output_parser) or not output_parser.link(rtsp_sink):
        raise RuntimeError("Unable to link packet publisher pipeline")

    loop = GLib.MainLoop()
    exit_code = 0
    output_frame_number = 0
    current_frame_index = -1
    next_reader_index = -1
    last_status_at = 0.0
    frame_timing_samples: deque[dict[str, float | int]] = deque(maxlen=60)

    def publish_status(*, ready: bool, force: bool = False) -> None:
        nonlocal last_status_at
        if status_path is None:
            return
        now = time.time()
        if not force and now - last_status_at < 0.50:
            return
        _write_status(
            status_path,
            {
                "schema_version": 1,
                "camera_id": camera_id,
                "sync_group": sync_group,
                "publisher_mode": "packet_copy",
                "period_seconds": sync_period_seconds,
                "epoch_seconds": sync_epoch_seconds,
                "normalized_phase": (
                    (now - sync_epoch_seconds) % sync_period_seconds
                )
                / sync_period_seconds,
                "current_frame_index": current_frame_index,
                "frame_count": frame_count,
                "output_frame_count": output_frame_number,
                "frame_timing_samples": list(frame_timing_samples),
                "timeline_timestamp": now,
                "updated_at": now,
                "pid": os.getpid(),
                "ready": ready,
            },
        )
        last_status_at = now

    def seek_reader(frame_index: int) -> bool:
        position = frame_index * Gst.SECOND // fps
        result = reader.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT | Gst.SeekFlags.ACCURATE,
            position,
        )
        if result:
            reader.set_state(Gst.State.PLAYING)
        return bool(result)

    def push_packet() -> bool:
        nonlocal current_frame_index, exit_code, next_reader_index, output_frame_number
        desired_index = frame_index_for_timestamp(
            time.time(), sync_period_seconds, frame_count, sync_epoch_seconds
        )
        if next_reader_index != desired_index:
            if not seek_reader(desired_index):
                print("packet publisher timeline seek failed", file=sys.stderr, flush=True)
                exit_code = 1
                loop.quit()
                return GLib.SOURCE_REMOVE
            next_reader_index = desired_index
        sample = app_sink.emit("try-pull-sample", Gst.SECOND)
        if sample is None:
            if repeat and seek_reader(desired_index):
                sample = app_sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                print("packet publisher could not read H.264 access unit", file=sys.stderr, flush=True)
                exit_code = 1
                loop.quit()
                return GLib.SOURCE_REMOVE
        source_buffer = sample.get_buffer()
        if source_buffer is None:
            exit_code = 1
            loop.quit()
            return GLib.SOURCE_REMOVE
        buffer = source_buffer.copy_deep()
        buffer.pts = output_frame_number * Gst.SECOND // fps
        buffer.dts = buffer.pts
        buffer.duration = Gst.SECOND // fps
        buffer.offset = output_frame_number
        result = app_source.emit("push-buffer", buffer)
        if result != Gst.FlowReturn.OK:
            print(f"packet publisher push failed: {result}", file=sys.stderr, flush=True)
            exit_code = 1
            loop.quit()
            return GLib.SOURCE_REMOVE
        output_timestamp = time.time()
        frame_timing_samples.append(
            {
                "rtp_timestamp": int(
                    (buffer.pts * 90_000 // Gst.SECOND) & 0xFFFFFFFF
                ),
                "capture_timestamp": output_timestamp,
                "output_timestamp": output_timestamp,
                "output_pts_ns": int(buffer.pts),
            }
        )
        output_frame_number += 1
        current_frame_index = desired_index
        next_reader_index = (desired_index + 1) % frame_count
        publish_status(ready=True)
        return GLib.SOURCE_CONTINUE

    def on_message(_bus: Gst.Bus, message: Gst.Message) -> None:
        nonlocal exit_code
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(
                f"packet publisher GStreamer error: {error}; debug={debug}",
                file=sys.stderr,
                flush=True,
            )
            exit_code = 1
            loop.quit()

    for pipeline in (reader, publisher):
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_message)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.quit())

    reader.set_state(Gst.State.PAUSED)
    reader.get_state(5 * Gst.SECOND)
    initial_index = frame_index_for_timestamp(
        time.time(), sync_period_seconds, frame_count, sync_epoch_seconds
    )
    if not seek_reader(initial_index):
        reader.set_state(Gst.State.NULL)
        raise RuntimeError("Unable to seek packet fixture to shared timeline")
    current_frame_index = initial_index
    next_reader_index = initial_index
    publisher.set_state(Gst.State.PLAYING)
    GLib.timeout_add(max(1, round(1000 / fps)), push_packet)
    try:
        loop.run()
    finally:
        publish_status(ready=False, force=True)
        app_source.emit("end-of-stream")
        publisher.set_state(Gst.State.NULL)
        reader.set_state(Gst.State.NULL)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--sync-period", type=float, required=True)
    parser.add_argument("--sync-epoch", type=float, default=0.0)
    parser.add_argument("--camera-id")
    parser.add_argument("--sync-group")
    parser.add_argument("--status-path", type=Path)
    args = parser.parse_args()
    return run(
        args.input,
        args.output,
        repeat=args.loop,
        fps=args.fps,
        sync_period_seconds=args.sync_period,
        sync_epoch_seconds=args.sync_epoch,
        camera_id=args.camera_id,
        sync_group=args.sync_group,
        status_path=args.status_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
