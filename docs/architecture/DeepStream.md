# Camera DeepStream production architecture

## 1. Quyết định kiến trúc

`app/` là source duy nhất của Camera DeepStream runtime.

Runtime giữ topology đơn giản và ổn định:

```text
app/config/*.yaml
        |
        v
app runner/supervisor
        |
        +-- một worker DeepStream cho mỗi camera
        |       |
        |       +-- capture/RTSP
        |       +-- person detection + application tracking
        |       +-- face / smoking / fire-smoke inference
        |       +-- event lifecycle + evidence reference
        |       +-- NVOSD annotation
        |       +-- RTSP publish
        |
        +-- MediaMTX RTSP/WebRTC/HLS
        +-- Dashboard API + static web
        +-- notification outbox worker
```

Mỗi camera có process worker riêng để cô lập state, model failure, track state
và restart. Không dùng một global tracker hoặc một event state dùng chung cho
nhiều camera.

DeepStream sở hữu inference, tracking, annotation và output frame. Dashboard
chỉ đọc live metadata, trạng thái runtime và event query; dashboard không tự vẽ
bbox lên live video.

## 2. Trạng thái audit hiện tại

`app/src/camera_safety/` hiện là production package; implementation DeepStream
được giữ dưới package boundary để characterization behavior không đổi.
Các rủi ro bắt buộc xử lý trước khi gọi production-ready:

| Mức | Vấn đề | Hậu quả |
|---|---|---|
| P0 | Working tree đang ở trạng thái move chưa hoàn tất; launcher, package script, test và một số config còn tham chiếu đường dẫn cũ | Có thể commit thiếu runtime hoặc khởi động nhầm source |
| P0 | `pipeline.py` gần 2.000 dòng, trộn GStreamer, model, tracking, event, evidence, notification, status và shutdown | Khó cô lập lỗi, khó test, dễ làm thay đổi một lane ảnh hưởng lane khác |
| P0 | Dashboard bind `0.0.0.0`, MediaMTX cho phép origin rộng, chưa có auth/TLS/authorization | Event, evidence và live endpoint có thể bị truy cập ngoài phạm vi |
| P1 | DeepStream worker legacy vẫn là compatibility implementation lớn trong package | Tiếp tục tách probe/orchestrator theo bounded change; không đổi event semantics |
| P1 | Dependency runtime DeepStream/GStreamer/pyds/CUDA chưa có manifest production đầy đủ | Không reproducible khi dựng máy mới |
| P1 | Config chứa path `/mnt/d`, `/opt`, camera IP và profile mock/prod trong cùng file | Deployment phụ thuộc máy phát triển, khó quản lý environment |
| P1 | Evidence, SQLite, JPEG annotation và thumbnail nằm chung một service/file layout | API, storage và lifecycle bị kết dính; đọc trên WSL mount dễ chậm |
| P1 | `RecognitionCore` và face engine cùng giữ state recognition | Có nguy cơ policy cadence/vote/lifecycle bị lệch |
| P1 | Face gallery nằm trong source tree | Dữ liệu sinh trắc học có nguy cơ bị commit và không có ACL/backup policy |
| P2 | `events.py` và `fire_smoke_events.py` có lifecycle riêng | Contract event bị phân mảnh, khó query thống nhất |

Static audit hiện tại của `app/`:

- Ruff: pass.
- YAML parse: pass.
- PowerShell parser: pass.
- Package/install, config merge, static checks và characterization tests đã pass.
- Docker và 30-second runtime acceptance vẫn chưa được công nhận khi chưa có
  Docker daemon/MediaMTX/DeepStream runtime evidence trên máy hiện tại.

## 3. Folder architecture production

```text
app/
├─ pyproject.toml
├─ README.md
├─ src/
│  └─ camera_safety/
│     ├─ __init__.py
│     │
│     ├─ domain/
│     │  ├─ contracts.py
│     │  ├─ detections.py
│     │  ├─ events.py
│     │  ├─ recognition.py
│     │  └─ tracking.py
│     │
│     ├─ application/
│     │  ├─ camera_worker.py
│     │  ├─ inference_orchestrator.py
│     │  ├─ event_orchestrator.py
│     │  ├─ evidence_service.py
│     │  └─ notification_service.py
│     │
│     ├─ adapters/
│     │  ├─ deepstream/
│     │  │  ├─ pipeline_builder.py
│     │  │  ├─ probes.py
│     │  │  ├─ metadata.py
│     │  │  └─ osd.py
│     │  ├─ models/
│     │  │  ├─ face_engine.py
│     │  │  ├─ smoking_engine.py
│     │  │  └─ fire_smoke_engine.py
│     │  ├─ media/
│     │  │  ├─ mediamtx.py
│     │  │  └─ mock_input.py
│     │  ├─ persistence/
│     │  │  ├─ event_repository.py
│     │  │  ├─ evidence_repository.py
│     │  │  └─ notification_outbox.py
│     │  └─ notifications/
│     │     ├─ telegram.py
│     │     └─ zalo.py
│     │
│     ├─ interfaces/
│     │  ├─ dashboard_api.py
│     │  ├─ event_queries.py
│     │  └─ health.py
│     │
│     └─ bootstrap/
│        ├─ config.py
│        ├─ paths.py
│        ├─ logging.py
│        └─ lifecycle.py
│
├─ web/
│  ├─ dashboard.html
│  ├─ dashboard.js
│  ├─ dashboard.css
│  └─ mediamtx_reader.js
│
├─ config/
│  ├─ base.yaml
│  ├─ dev.yaml
│  ├─ production.yaml
│  └─ cameras/
│     ├─ camera_face.yaml
│     ├─ camera_safety.yaml
│     └─ camera_dahua.yaml
│
├─ deploy/
│  ├─ wsl/
│  │  ├─ camera-safety.service
│  │  ├─ dashboard.service
│  │  └─ mediamtx.yml
│  ├─ powershell/
│  │  ├─ start.ps1
│  │  ├─ stop.ps1
│  │  └─ status.ps1
│  └─ models/
│     └─ manifest.yaml
│
└─ tests/
   ├─ unit/
   │  ├─ domain/
   │  ├─ application/
   │  └─ adapters/
   ├─ integration/
   └─ e2e/
```

## 4. Ownership boundary

### Domain

Chỉ chứa state và policy thuần, không import GStreamer, `pyds`, HTTP,
filesystem hoặc SQLite:

- `PersonTrack`, confirmation gate và geometry;
- `FaceRecognitionResult`, identity vote và unknown policy;
- `SafetyEvent`, lifecycle START/UPDATE/END;
- fire/smoke classification;
- typed event and evidence contracts.

### Application

Điều phối use case, nhưng không biết chi tiết DeepStream:

- nhận detection result từ adapter;
- cập nhật track state;
- mở/đóng event;
- chọn evidence frame;
- phát event sang repository và outbox;
- giữ invariant: dashboard/notification chỉ thấy event START hợp lệ.

### DeepStream adapters

Chỉ sở hữu:

- GStreamer element graph;
- pad probe và buffer mapping;
- tensor metadata;
- NVOSD object/display metadata;
- encoded output;
- GPU/model session lifecycle.

Pad probe không được tự ghi SQLite, gọi Telegram/Zalo hoặc tự scan evidence
directory.

### Persistence adapters

`EventRepository` lưu event index/query model. `EvidenceRepository` lưu:

- original annotated frame;
- card thumbnail;
- trace;
- manifest;
- idempotency record.

Dashboard đọc `EventRepository`/query API, không đọc `event.json` của từng thư
mục trong mỗi lần polling.

### Notification outbox

Notification chỉ nhận event đã có evidence reference. Provider adapter Telegram
hoặc Zalo không được truy cập trực tiếp pipeline state. Retry, cooldown,
idempotency và provider status nằm trong outbox.

## 5. Runtime data ngoài source

Source package không chứa model, face gallery hoặc evidence production.

```text
/opt/camera-safety/
├─ models/
├─ face_library/
├─ evidence/
├─ state/
├─ queue/
├─ logs/
└─ status/
```

Các thư mục trên phải có owner, permission, retention và backup policy riêng.
SQLite production không đặt trên thư mục source hoặc vùng mount dùng cho code.

## 6. Configuration contract

Config được merge theo thứ tự:

```text
base.yaml
    + profile dev hoặc production
    + camera definition
    + environment variables / secret file
    -> typed validated runtime config
```

Config không chứa credential plaintext và không hardcode đường dẫn máy phát
triển. Mỗi camera phải định nghĩa rõ:

- source type và URL;
- output path;
- enabled function;
- model lane;
- evidence policy;
- metadata endpoint;
- health/readiness policy.

Mock input chỉ nằm trong `dev.yaml`; production profile không được vô tình
khởi động mock camera.

## 7. Event and evidence flow

```text
frame PTS
  -> detection
  -> confirmed person track
  -> function result
  -> application event decision
  -> evidence capture
  -> event repository commit
  -> notification outbox
  -> dashboard query
```

Recognition contract:

- recognized event START dùng đúng frame tạo ra identity confirmation;
- unknown chỉ tạo khi track kết thúc và có face evidence hợp lệ;
- unknown dùng best face frame trong bounded track state, không dùng end frame;
- event list chỉ hiển thị START;
- END chỉ là lifecycle record nội bộ;
- thumbnail là bản dẫn xuất nhỏ, original frame giữ nguyên cho modal/report.

## 8. Deployment ownership

Production process ownership phải thuộc WSL service manager:

```text
camera-safety.service
  ├─ MediaMTX dependency
  ├─ dashboard API dependency
  └─ runner supervisor
       └─ one worker per camera
```

PowerShell chỉ là operator client gọi service `start`, `stop`, `status`; không
được giữ process ownership bằng foreground shell trap.

Readiness phải phân biệt:

- process tồn tại;
- model loaded;
- GPU provider active;
- input frame mới;
- output frame mới;
- analysis result không stale;
- dashboard/API reachable;
- evidence writable;
- notification outbox healthy.

## 9. Lộ trình migration

### Phase 0 — Canonicalize source

1. Chốt `app/` là source duy nhất.
2. Sửa package scripts, launcher, tests, docs và config để cùng dùng `app/`.
3. Xóa toàn bộ tham chiếu tới thư mục không còn canonical.
4. Thêm `app/src/camera_safety/__init__.py` và import package chuẩn.
5. Đưa face gallery và model ra runtime volume.
6. Chỉ hoàn tất khi clean checkout có thể cài và import package.

### Phase 1 — Package and dependency reproducibility

1. Tạo `app/pyproject.toml` với dependency runtime và test riêng.
2. Pin Python, DeepStream, GStreamer, CUDA, TensorRT, ONNX Runtime và MediaMTX.
3. Tạo model manifest có checksum và version.
4. Thêm config validation trước khi worker khởi động.

### Phase 2 — Tách domain/application

1. Tách geometry, tracking, recognition và lifecycle thành pure modules.
2. Tạo typed contracts thay cho dict tự do giữa pipeline và event store.
3. Giữ nguyên behavior hiện tại qua characterization tests.

### Phase 3 — Tách adapters

1. Tách DeepStream pipeline builder và pad probes.
2. Tách từng model engine thành adapter.
3. Tách MediaMTX/mock input và runtime status.
4. `camera_worker.py` chỉ compose các adapter và application service.

### Phase 4 — Persistence/API

1. Tách event repository, evidence repository và notification outbox.
2. Tạo read model cho dashboard.
3. Thumbnail/original dùng endpoint riêng với immutable cache.
4. Không để HTTP handler scan toàn bộ evidence tree trong request path.

### Phase 5 — Production lifecycle and security

1. Chuyển runner sang WSL service manager.
2. Bind dashboard nội bộ và đặt reverse proxy/auth/TLS.
3. Khóa MediaMTX origin và endpoint access.
4. Thiết lập retention, backup, disk-full handling và log rotation.

## 10. Acceptance bắt buộc

Không gọi migration hoàn tất chỉ vì unit test hoặc dashboard HTTP 200 pass.

Production gate phải có:

- clean checkout và package install pass;
- toàn bộ unit/integration test chạy trên `app`;
- config production validation pass;
- cold start với một worker mỗi camera;
- GPU provider và model checksum được ghi nhận;
- input/output frame freshness pass;
- event/evidence đọc lại được sau restart;
- thumbnail và original endpoint có cache contract đúng;
- notification outbox retry/idempotency pass;
- auth/TLS/network exposure pass;
- không còn source/test/launcher tham chiếu thư mục không canonical.
