# LS-Vision DeepStream production architecture

Ngày cập nhật: 21/08/2026

Tài liệu này là source of truth cho kiến trúc và lộ trình của runtime Camera
DeepStream. `server/` là boundary ADAS/FTP/archive độc lập và không thuộc
restructure này. `frigate/` không phải startup path, media owner, event store
hoặc test gate của LS-Vision.

## 1. Quyết định kiến trúc

`app/` là source canonical duy nhất của Camera DeepStream runtime. Python
runtime modules nằm trực tiếp dưới `app/src`; tên triển khai Docker là
`ls-vision`.

```text
Development (native WSL)
  app/config/dev.yaml
        -> runner
        -> một worker cho mỗi camera
        -> MediaMTX + dashboard

Production (Docker Desktop / WSL2 / NVIDIA)
  ls-vision Compose project
    ├─ ls-vision
    │   ├─ dashboard/API
    │   ├─ supervisor
    │   └─ một worker DeepStream cho mỗi camera
    └─ mediamtx
        └─ RTSP / WebRTC / HLS
```

Một worker sở hữu state của đúng một camera: capture, DeepStream graph,
tracking, function analysis, event/evidence dispatch, annotation và RTSP
output. Supervisor sở hữu lifecycle/restart worker; container sở hữu dashboard
và supervisor. Không dùng một global tracker hoặc event state dùng chung giữa
các camera.

Dashboard chỉ đọc runtime status, live metadata và event query. Dashboard không
tự vẽ bbox lên live video, không glob evidence tree trong request path và không
đọc trực tiếp từng `event.json`.

## 2. Audit trạng thái hiện tại

### Đã hoàn tất và có evidence

| Hạng mục | Trạng thái thực tế |
|---|---|
| Canonical source | `app/src/` đã là package runtime; launcher/package scripts/docs/tests đã chuyển sang boundary mới |
| Test ownership | Test DeepStream hiện hành ở `app/tests`; test Frigate/legacy cũ đã được xóa khỏi gate Camera |
| Config | Có `base.yaml`, `dev.yaml`, `production.yaml`, `e2e.yaml`; merge/duplicate ID/URL/mock production/model path được validate trước startup |
| Docker identity | Compose project/service/image dùng `ls-vision`; MediaMTX là service riêng |
| Docker image | Build pass với DeepStream `7.1-gc-triton-devel` đã pin digest; image tag `ls-vision:deepstream-7.1-gc-triton` |
| Runtime storage | Models/face library read-only; evidence/state/queue/logs là Docker-managed Linux volumes với prefix `ls-vision_` |
| Mock E2E | 30-second E2E pass: 3 worker, dashboard live/ready, freshness, restart, event API và evidence API |
| Media output E2E | HLS manifest thật từ MediaMTX đã trả HTTP 200 cho các output mock; không chỉ kiểm tra process tồn tại |
| Static/package checks | Root pytest `51 passed`; Ruff, compileall, Compose config và `git diff --check` pass |

Report runtime gần nhất: `.tmp/ls-vision-e2e/final-summary.json` với
`accepted=true`.

### Còn mở trước khi gọi production-ready

| Mức | Khoảng trống | Gate đóng |
|---|---|---|
| P0 | Production model volume chưa có checksum đầy đủ; `manifest.yaml` còn `sha256: ""` | Nạp đúng model vào volume, verify checksum và ghi nhận GPU/provider/model loaded |
| P0 | E2E hiện dùng `profile: e2e` và fixture mock, bỏ qua model validation khi tất cả source là mock | Chạy production profile với model thật và camera RTSP thật |
| P1 | `application/camera_worker.py` vẫn là compatibility implementation lớn; các adapter/application boundary đã có nhưng chưa tách hết logic | Tách probe/orchestrator/evidence/notification mà không đổi event semantics |
| P1 | Dashboard/API chưa có authentication/authorization/TLS operator | Bind/reverse proxy/auth/TLS và test endpoint access |
| P1 | Chưa có acceptance notification provider thật, retry/cooldown/idempotency sau restart | Test outbox với provider sandbox hoặc fake server durable |
| P1 | Chưa có disk-full, retention, backup và log rotation policy thực thi | Fault/retention acceptance trên Docker volumes |
| P2 | Chưa xác nhận WebRTC browser flow end-to-end; E2E hiện bắt buộc HLS | Browser/WebRTC acceptance bằng client thật |
| P2 | Vị trí disk image của Docker Desktop trên Windows không được Docker CLI xác nhận | Operator xác nhận Docker Desktop Disk image location là ổ E |

Kết luận audit: migration package + Docker mock runtime đã có evidence; chưa
được gọi là production-ready cho đến khi các gate production bên trên pass.

## 3. Folder architecture hiện hành

```text
app/
├─ pyproject.toml
├─ README.md
├─ src/
│  ├─ domain/
│  │  ├─ contracts.py
│  │  ├─ detections.py
│  │  ├─ events.py
│  │  ├─ fire_smoke_events.py
│  │  ├─ recognition.py
│  │  └─ tracking.py
│  ├─ application/
│  │  ├─ camera_worker.py
│  │  ├─ inference_orchestrator.py
│  │  ├─ event_orchestrator.py
│  │  ├─ evidence_service.py
│  │  └─ notification_service.py
│  ├─ adapters/
│  │  ├─ deepstream/
│  │  │  ├─ pipeline_builder.py
│  │  │  ├─ probes.py
│  │  │  ├─ metadata.py
│  │  │  └─ osd.py
│  │  ├─ models/
│  │  │  ├─ contracts.py
│  │  │  ├─ face_engine.py
│  │  │  ├─ smoking_engine.py
│  │  │  └─ fire_smoke_engine.py
│  │  ├─ media/
│  │  │  ├─ mock_input.py
│  │  │  └─ gstreamer_mock_publisher.py
│  │  └─ persistence/
│  │     ├─ event_repository.py
│  │     ├─ evidence_repository.py
│  │     └─ notification_outbox.py
│  ├─ interfaces/
│  │  ├─ dashboard_api.py
│  │  ├─ event_queries.py
│  │  └─ health.py
│  ├─ bootstrap/
│  │  ├─ config.py
│  │  ├─ paths.py
│  │  ├─ logging.py
│  │  └─ lifecycle.py
│  ├─ runner.py
│  └─ container.py
├─ web/
│  ├─ dashboard.html
│  └─ mediamtx_reader.js
├─ config/
│  ├─ base.yaml
│  ├─ dev.yaml
│  ├─ production.yaml
│  ├─ e2e.yaml
│  └─ cameras/
├─ deploy/
│  ├─ docker/
│  │  ├─ Dockerfile
│  │  ├─ compose.yaml
│  │  ├─ compose.e2e.yaml
│  │  └─ mediamtx.yml
│  ├─ powershell/
│  └─ models/manifest.yaml
└─ tests/
   ├─ unit/
   ├─ integration/
   └─ e2e/
```

`camera_worker.py` là vùng cần tiếp tục thu nhỏ. Folder boundary hiện tại
không có nghĩa mọi policy đã là pure domain; acceptance phải kiểm tra import
graph và behavior trước khi chuyển tiếp logic.

## 4. Ownership boundary

### Domain

Domain giữ policy/state thuần và không import GStreamer, `pyds`, OpenCV model
session, HTTP, SQLite hoặc filesystem:

- tracking geometry và person confirmation;
- recognition vote/cadence/unknown policy;
- safety event lifecycle START/UPDATE/END;
- fire/smoke lifecycle;
- typed detection/event/evidence contracts.

### Application

Application điều phối:

```text
detection result
  -> track update
  -> recognition/function decision
  -> event transition
  -> evidence reference
  -> event repository + notification outbox
```

Application quyết định identity confirmation và event lifecycle. Face engine
chỉ load/process/close model và trả typed result; không tự publish event.

### DeepStream/model adapters

Adapter sở hữu GStreamer graph, pad probe, tensor metadata, NVOSD, encode/output
và model session lifecycle. Pad probe không được tự ghi SQLite, evidence JSON,
Telegram/Zalo hoặc scan evidence directory.

### Persistence/evidence

`EventRepository` sở hữu read model/query index. `EvidenceRepository` sở hữu
original annotated frame, thumbnail, trace, manifest và idempotency record.
SQLite production nằm trong Docker volume, không nằm trên `/mnt/d`.

### Notification

Notification chỉ nhận event đã có evidence reference từ outbox. Retry,
cooldown, idempotency và provider status không được nằm trong pad probe.

## 5. Runtime data và Docker storage

Trong container, runtime root hiện giữ compatibility path `/opt/camera-safety`:

```text
/opt/camera-safety/
├─ models/          read-only: ls-vision_camera_models
├─ face_library/    read-only: ls-vision_camera_face_library
├─ evidence/        writable: ls-vision_camera_evidence
├─ state/           writable: ls-vision_camera_state
├─ queue/           writable: ls-vision_camera_queue
├─ logs/            writable: ls-vision_camera_logs
└─ status/          worker status
```

Source code vẫn ở `D:\BusinessAnalyze\Camera`. Docker build cache, image
layers, container filesystem và named volumes phải do Docker Desktop quản lý
trên disk image đặt tại ổ E; Docker CLI chỉ hiển thị Linux mountpoint nên
operator phải kiểm tra setting này trong Docker Desktop.

Face gallery/model production không được commit vào source. Dữ liệu sinh trắc
học phải có permission, retention, backup và access policy riêng.

## 6. Configuration contract

Precedence:

```text
base.yaml
  -> dev.yaml hoặc production.yaml
  -> camera profile / camera override
  -> environment variables hoặc secret file
  -> validated per-camera runtime config
```

Validation bắt buộc:

- camera ID không trùng;
- source/output URL hợp lệ;
- production không được dùng mock source;
- model file tồn tại khi production hoặc khi ép `CAMERA_VALIDATE_MODELS=1`;
- provider GPU phù hợp khi function yêu cầu GPU;
- evidence/state writable;
- secret chỉ lấy từ environment/secret file, không ghi vào event/manifest/log.

`e2e.yaml` là profile kiểm thử riêng, dùng fixture mock và model-free worker;
không được dùng làm production config.

## 7. Event/evidence contract

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

Invariants không được đổi khi tiếp tục refactor:

- recognized START dùng đúng frame tạo ra identity confirmation;
- unknown dùng best face evidence trong bounded track state;
- event list chỉ hiển thị START;
- END là lifecycle record nội bộ;
- thumbnail tạo tại START và original chỉ tải khi mở detail;
- event/evidence có idempotency key và đọc lại được sau restart.

## 8. Deployment và lifecycle

### Production

`app/deploy/docker/compose.yaml` là deployment contract:

- service `ls-vision`: dashboard/API, supervisor và toàn bộ camera workers;
- service `mediamtx`: RTSP/WebRTC/HLS;
- NVIDIA GPU reservation cho `ls-vision`;
- image DeepStream tag + digest được pin;
- healthcheck và `restart: unless-stopped`;
- dashboard/MediaMTX chỉ bind localhost trong compose hiện tại;
- model/face library read-only, runtime data read-write volumes.

PowerShell chỉ là operator client:

```powershell
.\app\deploy\powershell\start.ps1 -Action start -Mode Production
.\app\deploy\powershell\status.ps1 -Mode Production
.\app\deploy\powershell\stop.ps1 -Mode Production
```

Không dùng foreground shell trap để sở hữu production process.

### Development

Native WSL vẫn được hỗ trợ qua `start.ps1 -Mode Dev`; không dùng Docker Compose
hoặc Frigate làm startup path development.

### Readiness

Health contract phải phân biệt:

- process alive;
- model loaded;
- GPU provider active;
- input frame fresh;
- output frame fresh;
- analysis result fresh;
- evidence writable;
- notification outbox healthy;
- MediaMTX stream ready.

## 9. Kế hoạch cập nhật sau audit

### Phase 0 — Canonical source: DONE

- `app/` là source duy nhất.
- Flat runtime imports đã chuyển sang các module trực tiếp dưới `app/src`.
- launcher, package scripts, docs và test path đã cập nhật.
- face library source tree và Frigate/legacy Camera tests đã loại khỏi source/gate.

### Phase 1 — Config/dependency/Docker: MOSTLY DONE

- `app/pyproject.toml`, profile config, validation và model manifest đã có.
- DeepStream image, MediaMTX image và image digest đã pin.
- Compose `ls-vision` + named volumes + GPU reservation + healthcheck đã có.

Việc còn lại: điền checksum model thật, tạo model-volume manifest release và
chứng minh production image load model/provider trên máy đích.

### Phase 2 — Domain/application: PARTIAL

- typed contracts, tracking, recognition và event modules đã có.
- characterization/unit/integration tests đã chuyển sang `app/tests`.

Việc còn lại: chuyển business decision còn nằm trong `camera_worker.py` vào
application/domain service, giữ differential tests cho event/evidence/unknown
policy và không đổi topology inference.

### Phase 3 — DeepStream adapters: PARTIAL

- pipeline builder/probe/metadata/OSD adapter boundary đã được tạo.
- model adapters có interface load/health/process/close.
- E2E mock dùng GStreamer publisher thật để kiểm tra HLS qua MediaMTX.

Việc còn lại: tách implementation legacy khỏi worker theo từng bounded change;
pad probe chỉ publish typed result/status, không sở hữu persistence/notification.

### Phase 4 — Persistence/API: PARTIAL

- event/evidence repository, outbox, event query và health interfaces đã có.
- dashboard metrics/events/evidence endpoints và thumbnail/original contract đã có.

Việc còn lại: read model hoàn chỉnh, immutable cache headers, auth/authorization,
concurrency/error policy và integration test với volume/restart thực tế.

### Phase 5 — Production operations/security: OPEN

- Compose lifecycle, restart policy, GPU reservation và operator wrappers đã có.

Việc còn lại: TLS/auth, origin policy production, secret handling, log rotation,
retention/backup, disk-full policy, model rollout/rollback và browser WebRTC
acceptance.

### Phase 6 — Real-camera acceptance: OPEN

- cold start bằng ba fixture mock đã pass.

Việc còn lại: chạy với RTSP camera thật, production model volume, đúng provider
GPU, xác nhận inference/event/evidence/notification semantics và giữ artifacts
failed run để audit.

## 10. Acceptance gates

### Đã pass

- clean package import và root test suite: `51 passed`;
- Ruff, compileall, Compose config và diff check;
- Docker image build `ls-vision:deepstream-7.1-gc-triton`;
- Compose startup và healthcheck của `ls-vision`/MediaMTX;
- một worker cho mỗi camera trong E2E mock;
- input/output freshness trong 30 giây;
- HLS manifest thật từ MediaMTX cho camera outputs;
- dashboard live/ready, event feed START-only;
- container restart và state/evidence API sau restart.

### Chưa pass / không được suy diễn

- production model checksum và model-loaded evidence;
- DeepStream inference thật với camera RTSP thật;
- accuracy/recall của face, smoking, fire/smoke;
- WebRTC browser playback;
- notification provider thật và retry/idempotency sau restart;
- authentication/authorization/TLS;
- retention, backup, disk-full và log rotation;
- xác nhận Docker Desktop disk image thực sự ở ổ E.

Chỉ gọi LS-Vision production-ready khi toàn bộ nhóm gate thứ hai có evidence
được lưu cùng release report; unit test, HTTP 200, image build hoặc container
healthy riêng lẻ không đủ.
