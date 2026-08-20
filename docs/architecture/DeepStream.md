# Standalone DeepStream Safety experiment

> Current runtime note: this document is the operational source of truth for the
> Camera workspace. `docs/architecture/Platform.md` describes the legacy Frigate
> integration direction and is not used by `deepstream_safety/start.ps1`.

Investigation findings, unresolved accuracy risks, and the prioritized handover
plan from 20/08/2026 are documented in
[`docs/DeepStream-Stability-Handover-2026-08-20.md`](../DeepStream-Stability-Handover-2026-08-20.md).

This experiment is independent from Frigate and Docker. It runs in the WSL2
`Ubuntu-22.04` distro, whose VHDX is stored on `E:\WSL\Ubuntu-22.04`.

## Architecture

```text
bucket11.mp4 --ffmpeg/RTSP--> MediaMTX safety_mock
                                      |
                               rtspsrc in Python
                                      |
                 nvstreammux -> person nvinfer -> Python ROI classifier
                                      |
                                  nvdsosd
                                      |
                            RTSP publish safety_bbox
```

The Python process is the only application pipeline. It runs the person detector
in DeepStream, copies the latest frame/ROI into a bounded analysis queue, and
keeps model inference and evidence writes out of the GStreamer streaming path.
It attaches a `smoking` `NvDsObjectMeta` only after temporal confirmation and
renders bbox/label with `nvdsosd` on the same output buffer that is encoded and
published. The dashboard does not draw a second Canvas overlay: REST metadata
is for monitoring/API consumers only. This avoids frame drift between HLS and
polled metadata.

The output path selects `nvv4l2h264enc` when the DeepStream host exposes it,
with `x264enc` as an explicit compatibility fallback. The RTSP publisher uses
TCP transport to avoid UDP packet loss on the local WSL boundary. DeepStream
inference and NVOSD remain GPU-backed.

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

The person infer config is generated explicitly in `/tmp` because `nvinfer`
requires a DeepStream config file. The behavior model is loaded directly by
ONNX Runtime from `assets/models/smoking_behavior/model.onnx`; the source of all
model, RTSP, dimensions, threshold, and metadata values remains
`deepstream_safety/config.yaml`.

## Face-recognition performance contract

Person and smoking inference remains frame-driven. Face detection and ArcFace
recognition are bounded per active person track: the default cadence is 400 ms
(clamped to the 300-500 ms target), and the cached name/score is displayed on
intermediate frames. Track state is discarded when the track ends.

Face detection runs only on the padded person ROI; there is no full-frame face
detector pass. This WSL configuration orders ArcFace providers as CUDA then
CPU; TensorRT remains supported as a configured option on hosts with its
provider plugins. The pipeline logs both available and active providers. A CPU warning is not
GPU acceptance; verify the runtime log contains an active
`CUDAExecutionProvider` or `TensorrtExecutionProvider` before claiming GPU
recognition.

After changing the WSL environment, install the DeepStream experiment
requirements and verify the provider list in the same environment:

```bash
python3 -m pip install -r /mnt/d/BusinessAnalyze/Camera/deepstream_safety/requirements.txt
python3 -c "import onnxruntime as ort; print(ort.get_available_providers())"
```

The launcher exports the CUDA/cuDNN/TensorRT library directories before
starting the pipeline. For a direct run, use the same loader path:

```bash
export LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cuda_nvrtc/lib:/usr/lib/wsl/lib:/usr/local/cuda/lib64:/usr/local/lib/python3.10/dist-packages/tensorrt_libs
python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py --config /mnt/d/BusinessAnalyze/Camera/deepstream_safety/config.yaml
```

## Multi-camera configuration

`deepstream_safety/config.yaml` uses a `cameras` list. Each entry is isolated
into its own worker process and may select a real RTSP source or a mock source:

```yaml
cameras:
  - id: camera_face
    source:
      type: rtsp
      url: rtsp://192.168.1.20:554/stream1
    output:
      rtsp_url: rtsp://127.0.0.1:8554/face_bbox
    functions:
      trace: true
      face_recognition: true
      smoking_behavior: false
  - id: camera_safety
    source:
      type: mock
      url: rtsp://127.0.0.1:8554/safety_mock
      mock_video: /mnt/d/BusinessAnalyze/Camera/assets/fixtures/mock_videos/smoker/samples/part1/bucket11.mp4
      loop: true
    output:
      rtsp_url: rtsp://127.0.0.1:8554/safety_bbox
    functions:
      trace: true
      face_recognition: false
      smoking_behavior: true
      fire_smoke: true
```

The launcher starts one isolated `pipeline.py --camera-id ... --run-id ...`
worker per entry. This prevents recognition state from being shared between
cameras while keeping all durable evidence under one run. The legacy single
`input`/`output` shape remains accepted for one-camera deployments. This is
process-isolated multi-camera support; it does not use one batched `nvstreammux`
inference graph.

Within each worker, function branches are selected independently from
`functions`: a face-only camera does not create the smoking behavior ROI branch.
Person detection/tracking is the shared prerequisite for person functions, while
`fire_smoke` analyzes the complete frame and does not depend on a person ROI.
The optional function stages and their state are not coupled.

For distant/small people, `smoking_behavior` pads each person track ROI, resizes
it to `224x224`, applies the ViT image normalization, and returns the smoking
probability. A positive requires two scores above the configured threshold in a
four-attempt window. The classifier session must report an active CUDA provider.

`fire_smoke` uses the YOLO checkpoint at `assets/models/fire_smoke/best.pt`,
exported to `best.onnx` because the WSL runtime does not carry PyTorch. It runs
full-frame through ONNX Runtime CUDA, with independent thresholds for `fire`
and `smoke`. Each class has its own camera-level lifecycle event and evidence
directory; class detections are temporally confirmed before an event is opened.

## Evidence contract

Each launcher invocation creates one run directory:

```text
.tmp/deepstream-safety/snapshots-acceptance-<run_id>/
  manifest.json
  events.jsonl
  index.sqlite3
  notifications.sqlite3
  <camera_id>/<function>/<event_id>/
    event.json
    trace.jsonl
    snapshots/<START|UPDATE|END>-*-full.jpg
    snapshots/<START|UPDATE|END>-*-roi.jpg
```

The `functions` map in `config.yaml` is the source of the function directory;
camera names are not used to decide which detector runs. Face and smoking
behavior have separate event IDs and can be linked with `person_track_id`.
`trace.jsonl` contains lifecycle records (`START`, sampled `UPDATE`, `END`),
not every inference. The event directory is immutable for the whole lifecycle;
classification is updated in `event.json` and the journal rather than by moving
directories. `index.sqlite3` claims the idempotency key before writing each
trace or image, so retries do not duplicate records. Face events start as
`pending` and finish as `recognized` or `unrecognized` in the same directory.

## Telegram/Zalo notifications

The standalone runtime owns notification delivery in
`deepstream_safety/notifications.py`. The Telegram and Zalo HTTP contracts are
adapted from the existing Frigate provider implementation; no Frigate process,
API, database, or media directory is used at runtime.

Delivery is queued only after an evidence-backed `START` or `END` artifact is
written. Each worker records provider, recipient, lifecycle, retry attempts,
status, and error in `notifications.sqlite3` in the same run directory. The
outbox is idempotent by run, event, lifecycle, provider, and recipient, and a
cooldown prevents repeated alerts for one camera/function. Provider failures do
not stop inference or remove event evidence.

The current severity policy is configured in `config.yaml`: fire is `critical`,
smoke and smoking are `high`, an unrecognized face is `medium`, and a
recognized face is `info`. Critical/high events go to Telegram and Zalo;
medium/info face events go to Telegram only. Events without a matching severity
rule are `low` and are not delivered.

Create `.env.local` from `.env.example`, then enable the channels and global
notification switch in `deepstream_safety/config.yaml`:

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ZALO_BOT_TOKEN=...
ZALO_CHAT_ID=...
NGROK_URL=https://your-public-https-origin.example
```

Telegram uploads the local annotated snapshot directly. Zalo sends a snapshot
only when `NGROK_URL` (or another public HTTPS origin serving this workspace)
is configured; otherwise it sends a text alert and keeps the event evidence
locally. Credentials are never written to the run manifest, runtime status, or
notification payload.

The runtime status JSON exposes only non-secret provider readiness and delivery
counters under `notifications`. A real provider send still requires an
explicit end-to-end check with valid credentials; unit tests use an HTTP mock
and do not prove delivery to an external Telegram/Zalo recipient.
