# Thiết kế Camera Safety Extension

Ngày cập nhật: 15/08/2026
Trạng thái: Safety integration matrix 6/6 pass; combined E2E đã có report Safety, nhưng tracker
topology hiện còn fail ở runtime/core gates và chưa được đánh dấu acceptance DONE

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

Chỉ giữ một file cấu hình runtime là `deploy/config.yaml`. Safety dùng camera external trong
cùng topology; không có file cấu hình Safety thứ hai.

| File | Sở hữu |
| --- | --- |
| `deploy/config.yaml` | Camera, go2rtc, replay, record, Review và topology của Frigate/Safety |

### 5.1 Frigate camera fragment

```yaml
go2rtc:
  streams:
    safety_camera:
      - "rtsp://{FRIGATE_DAHUA_USER}:{FRIGATE_DAHUA_PASSWORD}@192.168.100.229:554/cam/realmonitor?channel=3&subtype=1"

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

`media_mode: external` và `detect.enabled: false` xác định Frigate không tạo capture/detector/record
cho camera này. Safety inference và evidence là producer-owned.

### 5.2 Replay chỉ dành cho acceptance

Runtime thường không khai báo mock replay; `go2rtc` đọc Dahua channel 3 và Safety đọc lại
stream `safety_camera` qua Frigate go2rtc. E2E mới tạo overlay tạm thời:

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

E2E inject `bucket11.mp4` vào overlay bằng fixture builder, đổi stream Safety sang MediaMTX trong
thời gian test rồi khôi phục config Dahua. Không gọi kết quả này là fire/smoke accuracy.

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

### 8.1 Combined E2E với car + face + Safety

Combined E2E là entrypoint chuẩn; Safety, notification và ba rule canonical được bật trong cùng
một run:

```powershell
& $python -u tools/tests/e2e/run_platform_runtime_test.py `
  2>&1 | Tee-Object '.tmp\platform-runtime-safety-e2e.log'
```

Entry point này dùng `--topology tracker`, `--include-safety`, `--enable-notifications` và chỉ
enable `car_alert`, `face_recognition`, `smoking_alert`. Fixture được tạo thêm `safety_camera`
từ `bucket11.mp4`, trong khi car/face vẫn dùng direct source hiện có. Các rule phụ như
`car_license_plate` không được bật, nên một Event chỉ sinh một notification.

Mỗi run ghi `report.md` với section `Safety result — smoking`, gồm health, smoking `bbox` trong
`Event.data.draw.boxes`, ảnh annotated full-frame `snapshot-smoking-bbox.jpg` và ảnh crop
`snapshot-smoking-bbox-crop.jpg`, Event completed, API ↔ SQLite,
clean snapshot, clip và Review link. Các file Safety nằm ở `media/safety/` khi
đã thu được; nếu run fail, report vẫn ghi các check false và giữ nguyên artifact chẩn đoán.
`summary.json.accepted` là acceptance tổng thể, không thay thế `summary.json.safety` hoặc bảng
Safety trong report.

## 9. Trạng thái hiện tại

Đã xác minh với code/runtime ngày 15/08/2026:

- Manual Event API có create/end; create body không nhận image upload.
- Manual snapshot dùng current frame; latest-frame trả `X-Frame-Time`.
- `FrigateConfig` cấm unknown top-level field.
- Fragment `safety_camera` với `detect.enabled: false`, record và Review labels validate qua
  `FrigateConfig.model_validate` và `compile_topology`.
- `run.ps1` chỉ nhận `-ConfigFile`; Safety được suy ra từ camera external trong topology.
- Tracker runtime hiện không có go2rtc process/listener 8554.

Cập nhật triển khai P1–P3:

- Đã có `extension/safety` với strict config, ONNX smoking adapter, temporal gate, bounded RTSP
  reader, Frigate Manual Event client và healthcheck.
- Safety dùng cùng `deploy/config.yaml`, cùng replay topology và cùng Frigate producer ingress.
- Targeted Safety tests đạt `13 passed`; coverage hiện tập trung vào smoking threshold, temporal
  lifecycle, timeout reconciliation, fail-closed API, cleanup và bounded reader. `deploy/run.ps1 build` đạt và tạo image
  `camera-safety:overlay-3361a5100cfe`.
- Integration matrix thực tế qua Docker/Frigate đã chạy 6 case: startup/latest-frame, API–SQLite
  lifecycle, media/review API, Safety restart/reconcile, source disconnect/recovery và Frigate
  restart/no-duplicate. Cả 6/6 case pass trong
  `.tmp/safety-integration/20260815-135451/summary.json`.
- Event API–SQLite, clean snapshot WebP, clip thật, ReviewSegment readback, restart/reconcile,
  source fault recovery và Frigate restart đều pass. Manual Event dùng
  `/api/events/{id}/snapshot-clean.webp`; `/snapshot.jpg` là canonical-media contract riêng và
  không được dùng làm gate cho Safety V1 hiện tại.
- Combined report đã được tích hợp vào platform runtime. Run
  `.tmp/platform-runtime/20260815-214631-215/report.md` đã ghi section Safety và cho thấy health
  `true`, nhưng Event completed/API–SQLite/snapshot/clip/Review đều `false`. Nguyên nhân runtime
  cần xử lý riêng: `detected_frames_processor` có `KeyError: car_camera`, Safety stream vượt FPS
  limit và recording segment bị invalid dưới tải tracker + Safety. Vì vậy đây là report evidence,
  chưa phải acceptance pass.
- Runtime cleanup đã khôi phục Frigate mặc định nhưng `runtime_restored=false`: launcher không giữ
  default replay runtime ổn định đủ 10 giây. Diagnostic
  `.tmp/runtime/runtime-ready.json` ghi rõ `car_camera.process_fps=4.0`, dưới ngưỡng `4.5`; face,
  detector và restart count đều không phải nguyên nhân. Report này không được coi là acceptance DONE.

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
| `SafetyConfig` | Frigate go2rtc URL, model path/provider, camera map | Immutable config | Camera external phải có policy smoking |
| `load_config(path)` | UTF-8 YAML | `SafetyConfig` | Parse/validate một lần lúc startup |
| `validate_camera_keys(safety, frigate_names)` | Hai tập camera names | `None` hoặc lỗi | Safety cameras phải là tập con của Frigate cameras |
| `resolve_stream_url(config, camera)` | Frigate go2rtc base + stream name | RTSP URL | Không lặp RTSP credential trong Safety config |

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
| `deploy/reference/Dockerfile.safety` | Existing runtime base + Safety source | `camera-safety:current`; config/model không bake vào image |
| `deploy/reference/docker-compose.yml` | Safety image/config/model | Canonical Frigate config, private network, no published port |
| `deploy/run.ps1` | Canonical `-ConfigFile` | Validate topology, start, wait-ready và stop Safety |

Chỉ cần ba thay đổi public trong launcher:

| Function/change | Input | Output/logic |
| --- | --- | --- |
| `Test-SafetyConfig` | Selected Frigate config | Fail trước Compose khi camera/model/config không hợp lệ |
| `Get-ComposePrefix(...)` | Compiled topology | Dùng một Compose topology cho Frigate và producer |
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

#### [x] `tools/tests/e2e/run_safety_runtime_test.py`

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

#### [x] `tools/tests/e2e/run_platform_runtime_safety_test.py`

Combined E2E car + face + Safety. Đây là flow opt-in để kiểm tra report hợp nhất; Safety failure
không bị che bởi kết quả car/face và acceptance tổng thể vẫn phải qua các tracker/core/restore gates.

### 10.6 Thứ tự thực hiện

- [x] P1 — Smoking-only core: config label `smoking`, temporal gate, API client và targeted unit tests;
      không yêu cầu fire/smoke model.
- [x] P2 — Reader/app, Compose/launcher optional profile và launcher tests; giữ fire/smoke disabled mặc định.
- [ ] P3 — Chạy smoking mock bằng `best.onnx` + `bucket11.mp4`; dedicated integration matrix và
      Event/media đã pass, nhưng combined tracker runtime còn lỗi core/recording và restore/resource
      gate cần hoàn tất trước khi đánh dấu acceptance.
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
