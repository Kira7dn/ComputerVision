# Standalone DeepStream Safety experiment

This experiment is independent from Frigate and Docker. It runs in the WSL2
`Ubuntu-22.04` distro, whose VHDX is stored on `E:\WSL\Ubuntu-22.04`.

## Architecture

```text
bucket11.mp4 --ffmpeg/RTSP--> MediaMTX safety_mock
                                      |
                               rtspsrc in Python
                                      |
                 nvstreammux -> nvinfer -> tensor decode
                                      |
                                  nvdsosd
                                      |
                            RTSP publish safety_bbox
```

The Python process is the only application pipeline. It reads the smoking ONNX
model directly, attaches `NvDsObjectMeta` for each decoded box, renders the box
with `nvdsosd`, and publishes the same metadata on ZeroMQ at
`tcp://127.0.0.1:5555`.

The WSL2 runtime uses `x264enc` for the final RTSP publish because WSL does not
expose a usable V4L2 hardware encoder. DeepStream inference and NVOSD remain GPU
backed; a native Linux deployment can replace only this encoder with
`nvv4l2h264enc`.

## One-time WSL setup

DeepStream 7.1 and `pyds 1.2.0` are installed in WSL. Install the remaining
standalone media runtime once:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
sudo mkdir -p /opt/camera-deepstream/mediamtx
cd /opt/camera-deepstream/mediamtx
sudo curl -fL -o mediamtx.tar.gz \
  https://github.com/bluenviron/mediamtx/releases/download/v1.19.3/mediamtx_v1.19.3_linux_amd64.tar.gz
sudo tar -xzf mediamtx.tar.gz mediamtx
sudo tee mediamtx.yml >/dev/null <<'YAML'
logLevel: info
rtsp: true
rtspAddress: :8554
paths:
  all_others: {}
YAML
```

## Start and stop

From PowerShell at the workspace root:

```powershell
.\deepstream_safety\start.ps1 start
ffplay rtsp://127.0.0.1:8554/safety_bbox
.\deepstream_safety\start.ps1 status
.\deepstream_safety\start.ps1 stop
```

The input is `assets/fixtures/mock_videos/smoker/samples/part1/bucket11.mp4`.
The output is the annotated RTSP stream `safety_bbox`. Logs are stored inside
the WSL runtime at `/opt/camera-deepstream/logs`.

## Direct runner

The process can also be started directly inside WSL:

```bash
python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py \
  --config /mnt/d/BusinessAnalyze/Camera/deepstream_safety/config.yaml
```

The infer config is generated explicitly in `/tmp` because `nvinfer` requires a
DeepStream config file. The source of all model, RTSP, dimensions, threshold,
and metadata values remains `deepstream_safety/config.yaml`. The TensorRT engine
is a one-time runtime artifact at `/opt/camera-deepstream/models/safety-smoking.engine`
and is not part of the workspace source tree.
