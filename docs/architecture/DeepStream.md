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
        -> hot_reload.py supervisor
        -> runner + dashboard/API
        -> một worker cho mỗi camera
        -> MediaMTX + Vite HMR dashboard

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
| Static/package checks | Root pytest `45 passed`; compileall, Vite lint/build, Compose config và `git diff --check` pass |
| Dashboard migration | `app/web` đã chuyển sang Vite + React + TypeScript + Tailwind v4 + shadcn/ui; API polling giữ state qua transient failure |
| Jetson hot reload | `npm run dev` chạy source sync, SSH tunnel và Vite HMR; thay đổi backend/config được restart trên isolated Jetson service |
| Browser dashboard check | 3 camera cards, event table và event detail dialog hoạt động; HLS fallback ổn định 3/3 camera, không còn CORS lỗi |

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
| P2 | WebRTC chưa thắng primary path trong browser hiện tại; dashboard đang fallback HLS ổn định 3/3 | Browser/WebRTC acceptance bằng client thật |
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
│  │  ├─ smoking_events.py
│  │  ├─ fire_smoke_events.py
│  │  ├─ recognition.py
│  │  └─ tracking.py
│  ├─ application/
│  │  ├─ camera_worker.py
│  │  ├─ analysis_scheduler.py
│  │  ├─ safety_replay.py
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
│  ├─ package.json
│  ├─ vite.config.ts
│  ├─ public/
│  │  └─ mediamtx_reader.js
│  └─ src/
│     ├─ App.tsx
│     ├─ components/
│     ├─ hooks/
│     ├─ lib/
│     └─ types.ts
├─ config/
│  ├─ base.yaml
│  ├─ dev.yaml
│  ├─ production.yaml
│  ├─ e2e.yaml
│  └─ cameras/
├─ deploy/
│  ├─ dev/
│  │  └─ hot_reload.py
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

Safety analysis hiện dùng một latest-only executor riêng cho từng function:

```text
NvDsFrameMeta(frame_num, buf_pts, ntp_timestamp)
  -> AnalysisSample + FrameKey
  ├─ face_recognition executor
  ├─ smoking_behavior executor (confirmed person ROI cùng frame)
  └─ fire_smoke executor (toàn frame, kể cả persons=[])
       -> stale/out-of-order gate
       -> function event store
       -> evidence + notification
```

Mỗi queue có kích thước 1 và drop frame trung gian khi model chậm. Kết quả
không được cập nhật event nếu quá `analysis.result_max_age_ms` hoặc có PTS/frame
order không tăng. Cache overlay tách theo function; chỉ fresh smoking inference
được chuyển vào episode store, còn fire/smoke overlay hết hạn độc lập.

Smoking dùng `person track ID` của DeepStream làm identity owner:

```text
full frame + confirmed person ROI
  ├─ stateless person-crop classifier (threshold 0.60, padding 20%)
  └─ T-Box ONNX detectors (Cigarette/Smoking, threshold 0.35)
       -> spatial association về person track
       -> canonical smoking signal
  -> per-person SmokingEpisodeStore
  -> CANDIDATE -> CONFIRMED -> CLEARING -> CLOSED
  -> evidence -> delayed NOTIFY
```

Mỗi person có episode riêng và event ID deterministic theo worker epoch,
person track ID và episode sequence. Candidate đạt `2/4` fresh score và tồn tại
ít nhất 0,4 giây mới được vẽ hoặc tạo `START`; START dùng đúng best frame/bbox
trong confirmation window. Bốn fresh negative liên tiếp hoặc person biến mất
sẽ chuyển sang `CLEARING`. Person tái xuất hiện positive trong 3 giây tiếp tục
cùng event; hết grace mới tạo `END`. Invalid crop, cached overlay, stale và
out-of-order result không được tính hit/miss. Notification chỉ phát một lần sau
khi episode tồn tại 3 giây; candidate không tạo event, overlay hay notification.

Hai detector T-Box lấy từ LeOS T-Box commit
`0dc3dde2e6f5d998886c6dc18371c7beab2d3343`, export ONNX opset 17 và dùng
cùng ONNX Runtime provider policy với LS-Vision. Chaitanya `Cigarette` và Soham
`Smoking` được normalize thành smoking; các class DMS khác không đi vào
lifecycle. Object inference chạy một lần trên full frame rồi ghép về person
track, không lặp hai model cho từng person crop. Global `AlertSmoother 3/2` của
T-Box không được dùng; per-person episode M-of-N vẫn là temporal owner duy nhất.

Fire/smoke không được suppress person confirmation, face recognition hoặc
smoking ROI. Overlap chỉ là correlation metric
`person_fire_smoke_overlap_count`.

Fire/smoke lifecycle là `detect -> region track -> dynamics verify -> event`.
Detection được ghép độc lập theo class và spatial region; mỗi region có ID và
một event riêng. Candidate phải đạt detector `4/6` và tồn tại ít nhất 1,5 giây
trước khi được vẽ hoặc tạo `START`. Dynamics `3/5` vẫn được tính vào event và
runtime metrics ở mode `advisory`, nhưng không chặn confirmation vì replay cho
thấy hard gate motion vừa tăng false negative vừa không loại hết hard-negative.
Track đã confirm chuyển sang `CLEARING` khi mất detection, được reacquire trong
3 giây với cùng event ID, và chỉ tạo `END` khi hết grace period. Cached overlay,
result stale hoặc out-of-order không được tiến lifecycle.

Fire/smoke `START` không gửi notification ngay. Region phải tồn tại tối thiểu
3 giây mới phát một `NOTIFY` nội bộ; transition này dùng event artifact đã có và
chỉ được phát một lần, kể cả khi track đi qua `CLEARING` rồi reacquire. Dữ liệu
dynamics advisory được giữ để mine hard-negative và huấn luyện verifier crop
ở phase sau. Production chỉ thay đổi policy này sau acceptance camera thật tối
thiểu 8 giờ và controlled true-fire test có latency không quá 3 giây.

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

```yaml
analysis:
  result_max_age_ms: 1000
  functions:
    fire_smoke:
      interval_ms: 300
      thresholds: {fire: 0.30, smoke: 0.40}
      rois: {}
    smoking:
      interval_ms: 400
      threshold: 0.60
      crop: {strategy: person_padded, padding_ratio: 0.20}
      confirmation: {hits: 2, attempts: 4, clear_hits: 4}
      temporal: {minimum_duration_seconds: 0.4}
      lifecycle:
        candidate_timeout_seconds: 3.0
        clearing_seconds: 3.0
        notification_min_duration_seconds: 3.0
```

Các field trên được resolve riêng cho từng camera rồi normalize về model
adapter config. `camera_safety` giữ ROI burner/plume; `DMS` dùng
`rois: {}` và do đó fire/smoke chạy toàn frame.

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

PowerShell chỉ là operator client; production Docker chạy trực tiếp qua Compose:

```powershell
docker compose -f app\deploy\docker\compose.yaml up -d
docker compose -f app\deploy\docker\compose.yaml ps
docker compose -f app\deploy\docker\compose.yaml stop
```

Không dùng foreground shell trap để sở hữu production process.

### Development

Development chạy trên Jetson native qua `npm run dev`; không dùng WSL hoặc Frigate
làm startup path development.

Development runtime gồm Vite tại `http://127.0.0.1:5173/dashboard.html`, dashboard/API tại
`http://127.0.0.1:18080`, MediaMTX và một worker cho mỗi camera. Vite dùng HMR cho
`app/web`; `app/deploy/dev/jetson_sync.py` đồng bộ `app/src`, `app/config` và các runtime
assets lên Jetson, nơi service supervisor restart đúng API/runner hoặc MediaMTX.

`npm run dev` giữ terminal ở foreground và phát log source sync/Vite. Nhấn
`Ctrl+C` để dừng sync, SSH tunnel và Vite; service Jetson có thể được quản lý
riêng qua systemd.

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

- `FrameKey`, `AnalysisSample`, `FunctionResult`, tracking, recognition và event modules đã có.
- face, smoking và fire/smoke đã tách thành bounded latest-only executors;
  stale/out-of-order result bị chặn trước event mutation.
- hard exclusion giữa fire/smoke và person đã bỏ; fire/smoke tiếp tục chạy khi không có người.
- characterization/unit/integration tests đã chuyển sang `app/tests`.

Việc còn lại: chuyển callback commit/evidence còn nằm trong `camera_worker.py`
vào application service, giữ differential tests cho event/evidence/unknown
policy và không đổi topology inference.

### Phase 3 — DeepStream adapters: PARTIAL

- pipeline builder/probe/metadata/OSD adapter boundary đã được tạo.
- model adapters có interface load/health/process/close.
- E2E mock dùng GStreamer publisher thật để kiểm tra HLS qua MediaMTX.

Việc còn lại: tách implementation legacy khỏi worker theo từng bounded change;
pad probe chỉ publish typed result/status, không sở hữu persistence/notification.

Chưa chuyển fire/smoke sang full-frame `nvinfer` hoặc smoking sang SGIE.
TensorRT/SGIE chỉ được nhận sau fixture baseline và parity gate; không đổi
preprocessing, sigmoid smoking hoặc person crop để ép model vào backend mới.

### Phase 4 — Persistence/API: PARTIAL

- event/evidence repository, outbox, event query và health interfaces đã có.
- dashboard metrics/events/evidence endpoints và thumbnail/original contract đã có.

Việc còn lại: read model hoàn chỉnh, immutable cache headers, auth/authorization,
concurrency/error policy và integration test với volume/restart thực tế.

### Phase 5 — Dashboard/frontend và dev lifecycle: DONE cho dev, PARTIAL cho production

- Dashboard đã được scaffold bằng `create-vite@latest` với React + TypeScript.
- Tailwind v4 và shadcn/ui đã được tích hợp; camera cards, metrics, event table và event detail
  dialog dùng component typed dưới `app/web/src`.
- `package.json` chỉ có ba contract: `dev`, `check`, `deploy`; Dockerfile build Vite bundle
  trong multi-stage image.
- Jetson development có Vite HMR và backend source sync; thay đổi backend tạo runner PID mới
  trên isolated service.

Việc còn lại: xác nhận WebRTC browser primary path, production auth/TLS/origin policy và browser
acceptance với camera/model thật.

### Phase 6 — Production operations/security: OPEN

- Compose lifecycle, restart policy, GPU reservation và operator wrappers đã có.

Việc còn lại: TLS/auth, origin policy production, secret handling, log rotation,
retention/backup, disk-full policy, model rollout/rollback và browser WebRTC
acceptance.

### Phase 7 — Real-camera acceptance: OPEN

- cold start bằng ba fixture mock đã pass.

Việc còn lại: chạy với RTSP camera thật, production model volume, đúng provider
GPU, xác nhận inference/event/evidence/notification semantics và giữ artifacts
failed run để audit.

Fixture baseline command đã có:

```powershell
wsl.exe -d Ubuntu-22.04 -- bash -lc "cd /mnt/d/BusinessAnalyze/Camera && python3 app/tests/e2e/run_safety_fixture_replay.py --report .tmp/safety-replay/baseline.json"
```

Manifest dùng `bucket11`, `roomfire41`, `printer31` và annotation fire/smoke
trong `markup.json`. Lệnh `--validate-only` đã pass 3/3 case. CPU baseline tại
`.tmp/safety-replay/cpu-baseline.json` có `measurement_valid=true`, 180 sample,
`accepted=null`, macro-F1 `0.1778`; fire precision/recall `0.2238/0.8649`, smoke
precision/recall `1.0/0.0`, inference P50/P95 `0.1334/0.1783` giây. Precision
smoke bằng 1.0 ở đây là convention khi không có positive prediction, không phải
smoke model tốt; recall 0.0 mới là tín hiệu cần xử lý.

Baseline GPU chưa được tạo trong lần cập nhật này vì Python/ONNX Runtime WSL
không trả provider trong preflight có timeout. CPU report chỉ là quality
baseline, không phải GPU latency, VRAM, TensorRT parity hoặc production-camera
acceptance.

A/B report tại `.tmp/safety-replay/architecture-ab.json` so sánh `HEAD` với
worktree hiện tại và tách hai kết luận:

- `architecture_accepted=true`: executor đã độc lập, PTS/frame gate đã có,
  stale event bị chặn, cached person ROI lệch frame và hard overlap suppression
  đã bỏ, fire-without-person vẫn được giữ;
- `quality_improved=false`: candidate pass no-regression nhưng macro-F1 chỉ đổi
  từ `0.17778` thành `0.17877` (`+0.00099`, nằm trong tolerance `0.01`) và smoke
  recall vẫn `0.0`.

Vì vậy thay đổi này chứng minh behavior kiến trúc tốt hơn, chưa chứng minh raw
model accuracy tốt hơn.

## 10. Acceptance gates

### Đã pass

- clean package import và root test suite: `54 passed`;
- compileall, Vite lint/build, Compose config và diff check;
- safety replay manifest validation: 3/3 case; scheduler/config/replay metrics unit gates pass;
- CPU fire/smoke baseline: 180 annotated samples, report hợp lệ nhưng không có comparative acceptance;
- native WSL start/status/stop với Vite HMR và backend hot reload;
- backend file change đã tạo runner mới và dashboard API tiếp tục trả HTTP 200;
- Vite browser dashboard với 3 camera, event table và event detail dialog;
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
- WebRTC browser playback primary path (HLS fallback đã pass trong browser);
- notification provider thật và retry/idempotency sau restart;
- authentication/authorization/TLS;
- retention, backup, disk-full và log rotation;
- xác nhận Docker Desktop disk image thực sự ở ổ E.

Chỉ gọi LS-Vision production-ready khi toàn bộ nhóm gate thứ hai có evidence
được lưu cùng release report; unit test, HTTP 200, image build hoặc container
healthy riêng lẻ không đủ.
