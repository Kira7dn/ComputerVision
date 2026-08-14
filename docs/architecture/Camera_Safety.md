# Thiết kế Camera Safety Extension

Ngày cập nhật: 15/08/2026
Trạng thái: thiết kế V1, chưa triển khai

## 1. Kết luận

`camera-safety` là container tùy chọn chạy song song với Frigate trên cùng Docker host/network.
Service đọc restream của camera, chạy model Safety và gọi Manual Event API hiện có của Frigate.

```mermaid
flowchart LR
    Camera --> Go2rtc[Frigate go2rtc restream]
    Go2rtc --> Frigate[Frigate capture / record]
    Go2rtc --> Safety[camera-safety]
    Safety -->|create / end Manual Event| Frigate
    Frigate --> Event[Event / SQLite / Review / notification]
```

Ranh giới V1:

- không sửa `frigate/src/frigate`;
- không phụ thuộc tracker hoặc recognition;
- không thêm schema, API, protobuf/gRPC, mTLS, durable journal hoặc edge framework mới;
- Frigate vẫn sở hữu Event, SQLite, recording, Review UI và notification;
- Safety chỉ sở hữu đọc frame, inference, temporal gate và ID của Event đang mở.

Tracker và Safety là hai extension ngang hàng. Camera chỉ chạy capability được gán, nên camera
không cần Safety không phải chịu model hoặc GPU load của Safety.

## 2. Phạm vi thực tế

| Capability/profile | Quyết định |
| --- | --- |
| Smoking mock trên `bucket11.mp4` | V1 POC, dùng model có sẵn |
| Fire và smoke trên camera Frigate-contained | Capability kế tiếp, cần model riêng |
| Smoking production accuracy | Chưa claim; mock fixture đủ để kiểm tra pipeline/lifecycle |
| Tracker-only | Không bị ảnh hưởng |
| External tracker + Safety | Deferred vì Frigate có thể không có current frame đúng để tạo snapshot |
| Remote Safety node | Deferred vì internal API/authentication không phù hợp để expose từ xa |
| Nhận diện danh tính người hút thuốc | Ngoài V1; Manual Safety Event không tự kích hoạt recognition |

`bucket11.mp4` là fixture mock cho smoking. Với mục tiêu kiểm tra pipeline, không cần dựng bộ
positive/negative riêng: chỉ cần model trả candidate, Safety mở/kết thúc Event đúng và Frigate
readback đúng. Đây không phải bằng chứng accuracy production hoặc bằng chứng fire/smoke.

Safety là công cụ hỗ trợ giám sát, không thay thế hệ thống báo cháy được chứng nhận. Event cần được
xem như cảnh báo cần xác minh, không tự điều khiển thiết bị an toàn trong V1.

## 3. Runtime tối thiểu

```text
FrameReader → OnnxInference → TemporalGate → FrigateEventClient
```

Chỉ cần bốn trách nhiệm:

| Thành phần | Trách nhiệm |
| --- | --- |
| `FrameReader` | Đọc restream; live chỉ giữ frame mới nhất; replay đọc tuần tự đến EOF |
| `OnnxInference` | Trả `label`, `score`, `bbox` tùy chọn; không gọi API |
| `TemporalGate` | Chống candidate đơn frame; quản lý `IDLE/PENDING/ACTIVE` |
| `FrigateEventClient` | Gọi create/end và giữ `frigate_event_id` của Event active |

V1 dùng một reader và một inference worker cho mỗi camera. Chưa tách worker theo model, chưa dùng
message bus và chưa xây scheduler GPU. Chỉ thay đổi sau khi profiling chỉ ra bottleneck thật.

### 3.1 Input

- Production/integration: đọc `rtsp://frigate:8554/<stream>` để không mở thêm session trực tiếp tới
  camera.
- Acceptance model: có thể đọc MP4 trực tiếp; vòng lặp read-infer chạy tuần tự nên không cần một
  FIFO framework riêng.
- Mất live stream: retry với backoff ngắn và chuyển health sang `degraded`.
- Live inference chậm: bỏ frame cũ, không tạo backlog.

Safety không mặc định đọc từ tracker. Tracker runtime hiện không cung cấp go2rtc restream ổn định.

### 3.2 Model

V1 ưu tiên ONNX vì image hiện có OpenCV, Requests và ONNX Runtime với TensorRT/CUDA/CPU. Model phù
hợp nhất cho mock hiện tại là `assets/models/smoking/best.onnx`:

- YOLO11m export ONNX, input `[1, 3, 640, 640]`;
- một class gốc `cigarette`, adapter map thành Safety label `smoking`;
- output `[1, 5, 8400]` chưa NMS, nên adapter phải decode + NMS tối thiểu;
- model đã tồn tại trong workspace, không cần thêm dependency Torch/Ultralytics vào Safety image.

Repository chưa có fire/smoke model artifact. Khi cần fire/smoke, thêm model riêng cùng adapter
contract; không dùng `yolov9-t-320.onnx` vì đó là detector COCO chung và không có label cigarette/fire
smoke phù hợp.

Adapter chỉ cần output tối thiểu:

```python
Detection(label, score, bbox, observed_at)
```

Fire/smoke chạy theo scene hoặc zone, không cần `track_id`. Smoking model phát hiện cigarette
candidate; Safety temporal gate mới quyết định episode. Không gọi tracker hoặc Face Recognition.
Threshold mock chỉ để tạo candidate ổn định, không coi là production threshold.

### 3.3 Temporal gate

```text
IDLE → PENDING → ACTIVE → IDLE
```

- candidate liên tục trong `confirm_seconds` mới mở Event;
- mất candidate trong `clear_seconds` mới đóng Event;
- mỗi `(camera, label)` chỉ có một Event active;
- create dùng `duration: null`; clear gọi end bằng đúng Event ID trả về.

V1 chỉ giữ state trong memory. Khi startup, client tìm các Event `in_progress` có
`sub_label=camera-safety`, đóng Event cũ rồi đánh giá lại frame mới. Đây là cleanup tối thiểu, không
phải exactly-once. Durable journal chỉ xem xét nếu mất Event sau restart trở thành lỗi vận hành thật.

## 4. Tích hợp Frigate hiện có

Endpoint đã tồn tại:

```http
POST /api/events/{camera_name}/{label}/create
PUT  /api/events/{event_id}/end
```

V1 gọi `http://frigate:5000` trong private Compose network. Internal port có quyền admin và không
cần JWT; mapping phục vụ acceptance phải giữ loopback-only như `127.0.0.1:5001:5000`, không bind
ra LAN/Internet. Body create tối thiểu:

```json
{
  "sub_label": "camera-safety",
  "score": 0.91,
  "duration": null,
  "include_recording": true
}
```

Chỉ thêm `draw.boxes` khi model thật sự trả bbox đáng tin cậy. Client có bounded timeout/retry. Nếu
create timeout và không rõ Event đã được tạo hay chưa, query Event gần nhất trước khi gửi lại; không
tự sinh Frigate Event ID.

### Giới hạn phải chấp nhận trong V1

- Manual Event snapshot lấy current frame của Frigate, không nhận exact inference frame từ Safety.
- Endpoint latest-frame có thể trả ảnh fallback; readiness phải yêu cầu `X-Frame-Time > 0`.
- Manual Event không phát MQTT `/events`.
- Event chỉ xuất hiện đúng Review/notification path khi camera bật record và Review alert cho label.
- External tracker camera có thể có snapshot rỗng/stale, nên chưa thuộc acceptance.

## 5. Cấu hình tối thiểu

Giữ hai file, không thêm top-level `safety:` vào Frigate config vì schema dùng `extra="forbid"`.

| File | Sở hữu |
| --- | --- |
| `deploy/config.yaml` | Camera, go2rtc, record, Review và topology của Frigate |
| `deploy/safety.yaml` | Model, camera Safety, threshold và temporal policy |

### 5.1 Safety config

```yaml
frigate_url: http://frigate:5000
restream_url: rtsp://frigate:8554
model:
  path: /models/smoking/best.onnx
  providers: [TensorrtExecutionProvider, CUDAExecutionProvider, CPUExecutionProvider]

cameras:
  safety_camera:
    stream: safety_camera
    inference_fps: 1
    labels:
      smoking: {enabled: true, threshold: 0.10}
    confirm_seconds: 1.0
    clear_seconds: 5.0
```

Camera key trong Safety config phải tồn tại trong Frigate config. Threshold `0.10` chỉ phục vụ mock
với model hiện có và không phải production threshold. Fire/smoke sẽ dùng config/model riêng khi
capability đó được triển khai.

### 5.2 Frigate camera fragment

```yaml
go2rtc:
  streams:
    safety_camera:
      - rtsp://user:password@camera.local/live

cameras:
  safety_camera:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/safety_camera
          input_args: preset-rtsp-restream
          roles: [detect, record]
    live:
      streams:
        Main: safety_camera
    detect:
      enabled: false
      width: 1280
      height: 720
      fps: 5
    record:
      enabled: true
    review:
      alerts:
        enabled: true
        labels: [smoking]
      detections:
        enabled: false
```

`detect.enabled: false` tắt object inference của Frigate nhưng giữ capture/current-frame path qua
`detect` role. Safety inference vẫn chạy độc lập.

### 5.3 Replay và acceptance config

Không thêm `bucket11.mp4` vào `deploy/config.yaml` hiện tại vì file này dùng
`runtime.replay.loop: true` cho face/car. `deploy/config.safety-replay-smoker.yaml` dùng riêng để
kiểm tra video plumbing và chạy smoking mock với model đã có:

```yaml
runtime:
  replay:
    loop: false
    sources:
      safety_camera: assets/fixtures/mock_videos/smoker/samples/part1/bucket11.mp4

go2rtc:
  streams:
    safety_camera:
      - rtsp://mediamtx:18554/safety_camera
```

Các contract hiện có của `run.ps1` vẫn áp dụng: replay/camera/go2rtc phải cùng key, replay URL phải
đúng `rtsp://mediamtx:18554/<name>`, input phải H.264 và readiness runtime yêu cầu
`camera_fps/process_fps >= 4.5`.

POC E2E dùng `deploy/safety.yaml` với `/models/smoking/best.onnx` và xác nhận Event `smoking` từ
`bucket11.mp4`. Không gọi kết quả này là fire/smoke accuracy. Config fire/smoke riêng chỉ cần khi
capability đó được triển khai.

## 6. Rủi ro và quyết định giảm thiểu

| Rủi ro | Mức | Quyết định V1 |
| --- | :---: | --- |
| Smoking model mock có score không ổn định | Trung bình | Dùng threshold thấp chỉ cho mock; không claim production accuracy |
| Fire/smoke chưa có model | Cao | Giữ capability tắt; không dùng detector COCO chung thay thế |
| Model smoking có metadata/license Ultralytics AGPL-3.0 | Cao khi phân phối sản phẩm | Dùng cho mock nội bộ; kiểm tra license/model provenance trước production |
| Safety và Frigate/Tracker tranh GPU | Cao | Một worker/camera, FPS thấp; benchmark trước khi bật nhiều camera |
| Port 5000 là internal admin API | Cao | Service dùng private network; host mapping chỉ bind loopback |
| Snapshot lệch inference frame | Trung bình | Chấp nhận cho V1; UI dùng current frame, không hứa exact evidence |
| Stream mất hoặc model lỗi bị hiểu là “an toàn” | Trung bình | Health `degraded`; không phát kết luận negative khi input/inference lỗi |
| Create timeout tạo Event trùng hoặc Event bị mở lâu | Trung bình | Một in-flight request/label, query trước retry và startup cleanup |
| Mock threshold bị hiểu là production threshold | Trung bình | Gắn rõ `mock_only`; chỉ làm dataset gate khi bật fire/smoke production |

Không giải quyết trong V1: remote node, exact-frame upload, durable spool, HA, certificate rotation,
dynamic model assignment, cross-service correlation và automatic identity enrichment.

## 7. Nguyên tắc triển khai

Không copy `TrackerRuntime`, Norfair, PTZ, Frigate Event maintainer hoặc recognition code. Image
Safety có thể dùng cùng base runtime hiện tại và học theo convention Docker/Compose/entrypoint của
tracker để giảm packaging work, nhưng không tái sử dụng tracker pipeline. Tối ưu image size để sau.

Danh sách file, function và thứ tự triển khai nằm trong TODO ở cuối tài liệu để chỉ có một source
of truth cho implementation scope.

## 8. Acceptance V1

| Gate | Bằng chứng cần có |
| --- | --- |
| Config | Frigate và Safety config validate; camera key khớp |
| Runtime | Source/model/API health; latest frame có `X-Frame-Time > 0` |
| Temporal | Candidate ngắn không mở Event; confirm/clear mở và đóng đúng một Event |
| Integration | Event có đúng camera/label/sub-label trong API, SQLite và Review |
| Media | Recording/snapshot không rỗng trên Frigate-contained camera |
| Mock lifecycle | Model smoking tạo candidate; Event `smoking` mở/kết thúc đúng |
| Resource | FPS/latency đạt mục tiêu và không làm Frigate runtime mất readiness |

`bucket11.mp4` đủ cho mock lifecycle, không phải bằng chứng accuracy production. Healthy container
hoặc schema validation không đủ để kết luận integration DONE.

## 9. Trạng thái hiện tại

Đã xác minh với code/runtime ngày 15/08/2026:

- Manual Event API có create/end; create body không nhận image upload.
- Manual snapshot dùng current frame; latest-frame trả `X-Frame-Time`.
- `FrigateConfig` cấm unknown top-level field.
- Fragment `safety_camera` với `detect.enabled: false`, record và Review labels validate qua
  `FrigateConfig.model_validate` và `compile_topology`.
- `run.ps1` hỗ trợ config riêng qua `-ConfigFile`, nhưng chưa có Safety service/profile.
- Tracker runtime hiện không có go2rtc process/listener 8554.

Đã có smoking model artifact `assets/models/smoking/best.onnx`, nhưng chưa triển khai
`extension/safety`, Docker/launcher wiring hoặc Safety config thực tế; chưa có Safety unit,
integration hay E2E pass.

## 10. Audit và TODO triển khai MVP

### 10.1 Kết quả audit

| Hạng mục | Quyết định |
| --- | --- |
| Service Safety riêng, không sửa Frigate core | Giữ |
| Frigate restream + Manual Event API | Giữ |
| Bốn trách nhiệm reader/inference/gate/event client | Giữ |
| Config Safety tách khỏi Frigate config | Giữ |
| Startup đóng Safety Event cũ | Giữ ở mức một query + end; không durable journal |
| Một worker/camera, shared model lock, session/thread ownership chi tiết | Không khóa cứng trong tài liệu; chọn cách đơn giản khi implement |
| Private helper `_preprocess`, `_decode` và internal health-file protocol | Để implementation quyết định sau khi model được chọn |
| Scheduler, message bus, multiprocessing, hot reload, HA, remote node | Loại khỏi V1 |
| Smoking mock và nhận diện danh tính | Smoking mock là V1; identity enrichment không thuộc Safety |
| Danh sách hàng chục test node | Rút thành ba test files và các behavior gate bắt buộc |

MVP chỉ cần chạy một camera Safety trước. Config vẫn dùng map để không khóa schema vào một camera,
nhưng chưa tối ưu concurrency nhiều camera trước khi có benchmark.

### 10.2 Data contract

```python
Detection(
    label,        # fire | smoke | smoking
    score,        # 0..1
    bbox,         # normalized xyxy hoặc None
    observed_at,  # Unix seconds
)

HazardDecision(
    camera,
    label,
    active,       # desired state sau confirm/clear
    score,
    bbox,
)
```

Gate quyết định desired hazard state; worker mới đồng bộ state đó với Frigate Event ID. Nếu create
hoặc end lỗi, worker giữ state/ID hiện tại và retry có backoff. Cách này đủ tránh mất lifecycle mà
không cần transaction, queue bền hoặc exactly-once protocol.

### 10.3 Runtime files và public functions

#### [ ] `frigate/src/extension/safety/__init__.py`

Package marker, không có side effect khi import.

#### [ ] `frigate/src/extension/safety/config.py`

| Function/type | Input | Output | Logic |
| --- | --- | --- | --- |
| `LabelPolicy` | `enabled`, `threshold` | Validated policy | Threshold trong `[0,1]` |
| `CameraSafetyConfig` | Stream, FPS, labels, confirm/clear seconds | Validated camera policy | Giá trị dương, có ít nhất một label enabled |
| `SafetyConfig` | Frigate/restream URL, model path/provider, camera map | Immutable config | Unknown field làm validation fail |
| `load_config(path)` | UTF-8 YAML | `SafetyConfig` | Parse/validate một lần lúc startup |
| `validate_camera_keys(safety, frigate_names)` | Hai tập camera names | `None` hoặc lỗi | Safety cameras phải là tập con của Frigate cameras |
| `resolve_stream_url(config, camera)` | Restream base + stream name | RTSP URL | Không lặp RTSP credential trong Safety config |

#### [ ] `frigate/src/extension/safety/inference.py`

| Function/type | Input | Output | Logic |
| --- | --- | --- | --- |
| `Detection` | Label, score, bbox, timestamp | Immutable candidate | Không chứa Event/API state |
| `SafetyModel` | Frame + timestamp | `list[Detection]` | Protocol để unit test inject fake |
| `OnnxSafetyModel.build(config)` | Model path + provider order | Ready model + active provider | Load ONNX, validate tensor contract, fail startup khi không usable |
| `infer(frame, observed_at)` | OpenCV BGR frame | Candidate list | Model-specific preprocess/run/decode; loại NaN, label/box invalid |

Không chốt private preprocessing function trước khi biết input/output thật của model.

#### [ ] `frigate/src/extension/safety/events.py`

| Function/type | Input | Output | Logic |
| --- | --- | --- | --- |
| `TemporalGate.observe(camera, detections, now)` | Candidate của một frame + monotonic time | `list[HazardDecision]` | Confirm liên tục mới active; clear timeout mới inactive; state tách theo label |
| `TemporalGate.reset()` | Không có | Không có | Dọn state cho test/shutdown |
| `FrigateEventClient.probe_camera(camera)` | Camera name | Ready/not-ready | Latest frame phải HTTP 200 và `X-Frame-Time > 0` |
| `create_event(camera, decision)` | Active decision | Frigate Event ID | POST `duration:null`, `sub_label:camera-safety`; reconcile trước khi retry timeout mơ hồ |
| `end_event(event_id)` | Server-created Event ID | Success/error | PUT end; caller chỉ xóa ID sau success |
| `reconcile(camera_labels)` | Managed camera/labels | Không có | Startup tìm và end Event `in_progress` của đúng `camera-safety` producer |

#### [ ] `frigate/src/extension/safety/app.py`

| Function/type | Input | Output | Logic |
| --- | --- | --- | --- |
| `LatestFrameReader` | RTSP URL | Latest frame sample | Một bounded latest slot; reconnect; không queue backlog |
| `CameraWorker.run(stop_event)` | Config, reader, model, gate, API client | Event lifecycle + health state | Sample theo FPS → infer → gate → sync create/end |
| `CameraWorker.stop()` | Stop signal | Bounded shutdown | Release capture; best-effort end active Events |
| `SafetyApplication.start/stop()` | Validated config/signals | Running/stopped process | Wire một model và configured cameras; không chứa model logic |
| `healthcheck()` | Runtime health snapshot | Exit `0/1` | Ready chỉ khi model, source và Frigate API đều fresh |
| `main(argv)` | `run`, `validate`, `healthcheck` | Process exit code | CLI/entrypoint; validate không mở stream hoặc tạo worker |

V1 dùng blocking OpenCV/Requests và thread đơn giản nếu cần. Không đưa asyncio hoặc multiprocessing
vào trước khi profiling chứng minh cần thiết.

### 10.4 Model, config và deployment files

| File | Input | Output/logic |
| --- | --- | --- |
| `assets/models/smoking/best.onnx` | Model đã có | Mount read-only; map `cigarette` → `smoking`; decode output `[1,5,8400]` |
| `assets/models/fire-smoke.onnx` | Model tương lai | Chỉ thêm khi bật fire/smoke; không blocker cho smoking mock |
| `tools/fixtures/safety_ground_truth.yaml` | Optional production review | Không cần cho mock; chỉ dùng khi muốn claim accuracy |
| `deploy/safety.yaml` | Mẫu mục 5.1 | Model path/provider, per-camera label/threshold/temporal policy |
| `deploy/config.safety-replay-smoker.yaml` | `bucket11.mp4` | Video plumbing/smoking development, không phải fire/smoke accuracy |
| `deploy/config.safety-acceptance.yaml` | Future fire/smoke acceptance fixture | Chỉ thêm sau khi đã chọn model; one-camera, `loop:false`, record/Review enabled |
| `deploy/reference/Dockerfile.safety` | Existing runtime base + Safety source | `camera-safety:current`; config/model không bake vào image |
| `deploy/reference/docker-compose.yml` | Safety image/config/model | Optional `external-safety` profile, private network, no published port |
| `deploy/run.ps1` | Optional `-SafetyConfigFile` | Validate, start, wait-ready và stop Safety mà không đổi default runtime |

Chỉ cần ba thay đổi public trong launcher:

| Function/change | Input | Output/logic |
| --- | --- | --- |
| `Test-SafetyConfig` | Safety + selected Frigate config | Fail trước Compose khi camera/model/config không hợp lệ |
| `Get-ComposePrefix(..., ExternalSafety)` | Safety enabled flag | Thêm profile chỉ khi user truyền `-SafetyConfigFile` |
| `Wait-SafetyReady` | Camera list + timeout | Chờ model/source/API ready; timeout phải có diagnostic |

Không thêm command launcher mới. `dev-start/start/stop/build` dùng cùng flow hiện có; build Safety
chỉ khi người dùng gọi `build`.

### 10.5 Test files

#### [ ] `frigate/tests/test_safety_extension.py`

Input là fake model/capture/HTTP và controlled time. Phải chứng minh:

- config reject unknown/invalid value và camera mismatch;
- single-frame candidate không active; confirm/clear đúng thời gian;
- fire/smoke state độc lập;
- create dùng server Event ID; timeout mơ hồ không tạo duplicate;
- API/model/source error không bị hiểu thành `no_fire`;
- live reader không tạo unbounded backlog;
- shutdown/restart cleanup chỉ tác động Event có `sub_label=camera-safety`.

#### [ ] `tools/tests/unit/test_safety_launcher.py`

Input là temporary Frigate/Safety configs. Phải chứng minh optional profile, mount/env, mismatch
failure, readiness timeout diagnostic và PowerShell parser; Safety disabled không đổi tracker hoặc
recognition behavior.

#### [ ] `tools/tests/e2e/run_safety_runtime_test.py`

Public flow:

```text
prepare_run
→ start_runtime
→ wait_current_frame
→ observe_real_model_events
→ verify API/SQLite/Review/media
→ verify smoking lifecycle
→ collect resource metrics
→ restore runtime
→ write summary.json
```

POC E2E dùng ONNX smoking thật và `bucket11.mp4`; không cần ground-truth manifest. Output
`.tmp/safety-runtime/<run-id>/summary.json` chứa `accepted`, `measurement_valid`, Event IDs,
lifecycle/latency gates và `runtime_restored`. Fake model chỉ thuộc unit/integration test.

### 10.6 Thứ tự thực hiện

- [ ] P1 — Smoking-only core: config label `smoking`, temporal gate, API client và targeted unit tests;
      không yêu cầu fire/smoke model.
- [ ] P2 — Reader/app, Compose/launcher optional profile và launcher tests; giữ fire/smoke disabled mặc định.
- [ ] P3 — Chạy smoking mock bằng `best.onnx` + `bucket11.mp4`; kiểm tra Event/media/restore và resource gate.
- [ ] P4 — Chỉ khi cần fire/smoke: chọn model, kiểm tra ONNX I/O/license, thêm fixture validation nhỏ và
      benchmark trên thiết bị thật; chỉ sau gate này mới triển khai detector fire/smoke.

Fire/smoke là capability kế tiếp sau smoking mock. Không thêm Face/Recognition correlation trong
Safety V1.

### 10.7 Điều kiện hoàn thành

V1 chỉ DONE khi:

1. targeted unit/launcher tests pass;
2. Safety container ready với model thật;
3. Event create/end/readback và snapshot/recording pass;
4. smoking mock E2E có report hoàn chỉnh và `runtime_restored=true`;
5. resource gate không làm Frigate mất readiness;
6. fire/smoke chỉ được đánh dấu production-ready sau khi có model và dataset gate riêng.

Schema validation, image build, healthy container hoặc một positive clip riêng lẻ không đủ để kết
luận DONE.
