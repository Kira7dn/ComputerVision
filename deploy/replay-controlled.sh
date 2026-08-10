#!/bin/sh
set -eu

FFMPEG=/usr/lib/ffmpeg/7.0/bin/ffmpeg
FFPROBE=/usr/lib/ffmpeg/7.0/bin/ffprobe
SOURCE=/runtime/source
CAMERA=${1:?camera name is required}
FIFO=/tmp/replay-raw.yuv
BLACK=/tmp/replay-black.yuv
TRIGGER=/tmp/replay-trigger
STARTED=/tmp/replay-started
DONE=/tmp/replay-done

WIDTH=$($FFPROBE -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$SOURCE")
HEIGHT=$($FFPROBE -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$SOURCE")
FPS=$($FFPROBE -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$SOURCE")
FRAME_DELAY=$(awk -v fps="$FPS" 'BEGIN { split(fps,v,"/"); print v[2]/v[1] }')

rm -f "$FIFO" "$TRIGGER" "$STARTED" "$DONE"
mkfifo "$FIFO"

$FFMPEG -hide_banner -loglevel warning \
  -f rawvideo -pixel_format yuv420p -video_size "${WIDTH}x${HEIGHT}" \
  -framerate "$FPS" -i "$FIFO" -an -c:v libx264 -preset ultrafast \
  -tune zerolatency -g 15 -keyint_min 15 -sc_threshold 0 \
  -x264-params repeat-headers=1 -f rtsp -rtsp_transport tcp \
  "rtsp://mediamtx:18554/$CAMERA" &
PUBLISHER_PID=$!

trap 'kill "$PUBLISHER_PID" 2>/dev/null || true; wait "$PUBLISHER_PID" 2>/dev/null || true' EXIT INT TERM

# Keep one writer open for the lifetime of the publisher. Switching between
# standby and source therefore never closes the FIFO or the RTSP session.
exec 3>"$FIFO"

$FFMPEG -hide_banner -loglevel error -f lavfi \
  -i "color=c=black:s=${WIDTH}x${HEIGHT}:r=${FPS}" -frames:v 1 \
  -pix_fmt yuv420p -f rawvideo "$BLACK"

while kill -0 "$PUBLISHER_PID" 2>/dev/null; do
  if [ -s "$TRIGGER" ]; then
    TOKEN=$(cat "$TRIGGER")
    rm -f "$TRIGGER" "$DONE"
    printf '%s %s\n' "$TOKEN" "$(date +%s.%N)" >"$STARTED"
    $FFMPEG -hide_banner -loglevel error -re -i "$SOURCE" -map 0:v:0 \
      -an -vf "scale=${WIDTH}:${HEIGHT},fps=${FPS}" -pix_fmt yuv420p \
      -f rawvideo pipe:1 >&3
    printf '%s %s\n' "$TOKEN" "$(date +%s.%N)" >"$DONE"
  else
    cat "$BLACK" >&3
    sleep "$FRAME_DELAY"
  fi
done

wait "$PUBLISHER_PID"
