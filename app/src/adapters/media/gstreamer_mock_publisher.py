"""Publish an H.264 MP4 fixture to an RTSP server for E2E tests."""

from __future__ import annotations

import argparse
import signal
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def run(input_path: Path, output_url: str) -> int:
    Gst.init(None)
    pipeline = Gst.Pipeline.new("fixture-publisher")
    source = Gst.ElementFactory.make("filesrc", "source")
    demux = Gst.ElementFactory.make("qtdemux", "demux")
    queue = Gst.ElementFactory.make("queue", "queue")
    parser = Gst.ElementFactory.make("h264parse", "parser")
    sink = Gst.ElementFactory.make("rtspclientsink", "sink")
    elements = (pipeline, source, demux, queue, parser, sink)
    if any(element is None for element in elements):
        raise RuntimeError("GStreamer fixture publisher elements are unavailable")

    source.set_property("location", str(input_path))
    sink.set_property("location", output_url)
    sink.set_property("protocols", "tcp")
    for element in (source, demux, queue, parser, sink):
        pipeline.add(element)
    if not source.link(demux) or not queue.link(parser):
        raise RuntimeError("Unable to link fixture publisher elements")
    request_pad = sink.get_request_pad("sink_%u")
    if request_pad is None:
        raise RuntimeError("Unable to request rtspclientsink input pad")
    if parser.get_static_pad("src").link(request_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError("Unable to link H.264 parser to rtspclientsink")

    def on_demux_pad(_demux: Gst.Element, pad: Gst.Pad) -> None:
        target = queue.get_static_pad("sink")
        caps = pad.get_current_caps() or pad.query_caps(None)
        is_video = caps is not None and caps.get_size() > 0 and caps.get_structure(0).get_name().startswith("video/")
        if target is not None and is_video and not target.is_linked():
            pad.link(target)

    demux.connect("pad-added", on_demux_pad)
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            loop.quit()

    bus.connect("message", on_message)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.quit())
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        sink.release_request_pad(request_pad)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    return run(args.input, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
