# LS-Vision DeepStream runtime

Ngày cập nhật: 26/08/2026

## Runtime graph

```text
production.yaml
  -> service
      -> dashboard/API
      -> mock timeline runtime
          -> synchronized publisher for each non-media-only mock camera
      -> runner
          -> camera process per non-media-only camera
              -> capture/decode
              -> person tracking and function analyzers
              -> event/evidence transition
              -> annotation/encode
              -> RTSP output
      -> mock media server when configured
  -> native MediaMTX -> RTSP/HLS/WebRTC
```

Một camera process sở hữu state, model session, tracking, event transition và output của đúng camera đó. Synchronized mock timeline là media-plane state độc lập: worker chỉ subscribe raw RTSP và không được tự khởi động publisher. Runner sở hữu restart/backoff của worker; timeline failure được restart riêng và readiness fail closed cho tới khi group lock lại. Khi chỉ controller timeline restart, controller mới adopt publisher còn sống qua PID/status tươi; publisher chỉ bị dừng cùng process group khi toàn service dừng, nhờ đó camera worker không phải reconnect.

## Layers

- Domain không phụ thuộc DeepStream, HTTP hoặc persistence implementation.
- Application điều phối typed samples/results/transitions và stale ordering gate.
- DeepStream adapter sở hữu graph, probes, tensor metadata, OSD và encode.
- Persistence adapter sở hữu event journal, SQLite idempotency và evidence files.
- Notification adapter chỉ nhận event đã có evidence.
- Dashboard đọc bounded status/event projections; không chạy inference.

## Configuration

Hai YAML profile standalone. Production không kế thừa development. Environment paths được resolve qua một runtime-path owner; source URL, function flags và model policy vẫn được validate trước khi tạo process.

Mỗi resolved camera config được compile thành một `CameraExecutionPlan` bất biến. Plan khai báo function đang bật, shared node cần dùng, model revision SHA-256, interval, estimated inference rate và `plan_hash`; `FunctionRegistry` là mapping tường minh từ function sang config section, dependency, engine factory metadata, interval/model resolver và processor. Worker chỉ tạo model session và latest-only executor cho function nằm trong plan; person detector/CPU frame branch được dùng chung thay vì tạo lại theo từng function.

Runner theo dõi YAML và reconcile theo `plan_hash`: config candidate phải validate toàn bộ trước, camera không đổi giữ nguyên PID/state, chỉ camera có plan đổi mới bị restart. Candidate lỗi, kể cả YAML sai cú pháp, giữ nguyên generation đang chạy và xuất lỗi qua `runner.json`. Dashboard lấy topology/function runtime từ `active_cameras` do runner công bố, không đọc candidate bị từ chối như active state. Các trường thuộc synchronized mock timeline (`sync_group`, period, epoch, media/source URL) không được hot reload; runner fail closed và yêu cầu restart toàn service để bốn camera đổi clock atomically.

Mọi feature section (`recognition`, `dms`, `smoking_behavior`, `fire_smoke`, `front_assistance`) dùng cùng quy tắc deep-merge override tại camera. `runtime.dynamic_pipeline` giới hạn số function và tổng inference rate ước tính theo camera; khi `enforce=true`, plan vượt budget bị từ chối trước khi worker/model khởi động.

## Storage

```text
/opt/ls-vision/
├─ current -> releases/<release>
├─ releases/
├─ models/
├─ face_library/
├─ data/evidence/
├─ data/state/
├─ data/queue/
├─ data/logs/
└─ data/status/
```

Release source là immutable sau deploy. Data directories không nằm dưới release và không bị rollback.

## Readiness

Liveness chỉ chứng minh service HTTP sống. Readiness của vision camera yêu cầu process và output freshness; synchronized mock còn yêu cầu `mock-timeline.json` fresh và server-side group lock. Dashboard công bố `config_generation`, `plan_hash`, function/shared node/model revision và camera vừa restart từ `runner.json`. Browser acceptance kiểm tra đủ bốn member, p95 drift không quá 100 ms, max drift không quá 250 ms và re-lock trong 5 giây sau timeline restart.
