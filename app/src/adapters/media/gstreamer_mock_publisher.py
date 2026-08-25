"""Publish a bounded video fixture as a continuous live H.264 RTSP stream."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

import cv2
import gi

from domain.mock_timeline import frame_index_for_timestamp

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def make_element(factory: str, name: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer fixture publisher element is unavailable: {factory}")
    return element


def run(
    input_path: Path,
    output_url: str,
    *,
    repeat: bool = False,
    fps: int = 15,
    sync_period_seconds: float = 0.0,
    sync_epoch_seconds: float = 0.0,
) -> int:
    capture = cv2.VideoCapture(str(input_path))
    source_fps = float(capture.get(cv2.CAP_PROP_FPS))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    synchronized = sync_period_seconds > 0.0
    if synchronized and frame_count <= 0:
        capture.release()
        raise RuntimeError(f"Unable to determine fixture frame count: {input_path}")
    current_frame_index = (
        frame_index_for_timestamp(
            time.time(),
            sync_period_seconds,
            frame_count,
            sync_epoch_seconds,
        )
        if synchronized
        else 0
    )
    if current_frame_index:
        capture.set(cv2.CAP_PROP_POS_FRAMES, current_frame_index)
    ok, current_frame = capture.read()
    if not ok or current_frame is None:
        capture.release()
        raise RuntimeError(f"Unable to decode fixture: {input_path}")
    if source_fps <= 0:
        source_fps = float(fps)
    height, width = current_frame.shape[:2]

    Gst.init(None)
    pipeline = Gst.Pipeline.new("fixture-publisher")
    source = make_element("appsrc", "source")
    queue = make_element("queue", "queue")
    convert = make_element("videoconvert", "convert")
    raw_caps = make_element("capsfilter", "raw-caps")
    encoder = make_element("x264enc", "encoder")
    parser = make_element("h264parse", "parser")
    sink = make_element("rtspclientsink", "sink")

    source.set_property("is-live", True)
    source.set_property("block", True)
    source.set_property("format", Gst.Format.TIME)
    source.set_property(
        "caps",
        Gst.Caps.from_string(
            f"video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1"
        ),
    )
    raw_caps.set_property(
        "caps", Gst.Caps.from_string(f"video/x-raw,format=I420,framerate={fps}/1")
    )
    encoder.set_property("bitrate", 4_000)
    encoder.set_property("speed-preset", "ultrafast")
    encoder.set_property("tune", "zerolatency")
    encoder.set_property("key-int-max", 5)
    encoder.set_property("bframes", 0)
    parser.set_property("config-interval", -1)
    sink.set_property("location", output_url)
    sink.set_property("protocols", "tcp")

    elements = (source, queue, convert, raw_caps, encoder, parser, sink)
    for element in elements:
        pipeline.add(element)
    if any(
        not left.link(right)
        for left, right in zip(elements, elements[1:], strict=False)
    ):
        raise RuntimeError("Unable to link fixture publisher pipeline")

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    output_frame_number = 0
    source_advance = 0.0
    exit_code = 0

    def read_next_frame() -> bool:
        nonlocal current_frame, current_frame_index
        wrapped = False
        ok, frame = capture.read()
        if (not ok or frame is None) and repeat:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
            if ok and frame is not None:
                current_frame_index = 0
                wrapped = True
        if not ok or frame is None:
            return False
        current_frame = frame
        if not wrapped and current_frame_index + 1 < frame_count:
            current_frame_index += 1
        return True

    def align_to_shared_timeline() -> bool:
        nonlocal current_frame, current_frame_index
        desired_index = frame_index_for_timestamp(
            time.time(),
            sync_period_seconds,
            frame_count,
            sync_epoch_seconds,
        )
        if desired_index == current_frame_index:
            return True
        if desired_index == current_frame_index + 1:
            return read_next_frame()
        capture.set(cv2.CAP_PROP_POS_FRAMES, desired_index)
        ok, frame = capture.read()
        if not ok or frame is None:
            return False
        current_frame = frame
        current_frame_index = desired_index
        return True

    def push_frame() -> bool:
        nonlocal exit_code, output_frame_number, source_advance
        if synchronized and not align_to_shared_timeline():
            print("fixture publisher timeline seek failed", file=sys.stderr, flush=True)
            exit_code = 1
            loop.quit()
            return GLib.SOURCE_REMOVE
        if current_frame.shape[1] != width or current_frame.shape[0] != height:
            exit_code = 1
            loop.quit()
            return GLib.SOURCE_REMOVE
        payload = current_frame.tobytes()
        buffer = Gst.Buffer.new_allocate(None, len(payload), None)
        buffer.fill(0, payload)
        buffer.pts = output_frame_number * Gst.SECOND // fps
        buffer.dts = buffer.pts
        buffer.duration = Gst.SECOND // fps
        buffer.offset = output_frame_number
        result = source.emit("push-buffer", buffer)
        if result != Gst.FlowReturn.OK:
            print(f"fixture publisher push failed: {result}", file=sys.stderr, flush=True)
            exit_code = 1
            loop.quit()
            return GLib.SOURCE_REMOVE
        output_frame_number += 1

        # Preserve the fixture's real playback rate. A 10 FPS source is
        # duplicated at 20 FPS; a 30 FPS source is sampled down to 20 FPS.
        if not synchronized:
            source_advance += source_fps
            while source_advance >= fps:
                source_advance -= fps
                if not read_next_frame():
                    source.emit("end-of-stream")
                    return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def on_message(_bus: Gst.Bus, message: Gst.Message) -> None:
        nonlocal exit_code
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(
                f"fixture publisher GStreamer error: {error}; debug={debug}",
                file=sys.stderr,
                flush=True,
            )
            exit_code = 1
            loop.quit()
        elif message.type == Gst.MessageType.EOS:
            loop.quit()

    bus.connect("message", on_message)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.quit())
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add(max(1, round(1000 / fps)), push_frame)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        capture.release()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--sync-period", type=float, default=0.0)
    parser.add_argument("--sync-epoch", type=float, default=0.0)
    args = parser.parse_args()
    return run(
        args.input,
        args.output,
        repeat=args.loop,
        fps=args.fps,
        sync_period_seconds=args.sync_period,
        sync_epoch_seconds=args.sync_epoch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
