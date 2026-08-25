# LS-Vision DeepStream runtime

Ngày cập nhật: 25/08/2026

## Runtime graph

```text
production.yaml
  -> service
      -> dashboard/API
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

Một camera process sở hữu state, model session, tracking, event transition và output của đúng camera đó. Runner sở hữu restart/backoff; service owner thoát non-zero nếu một child quan trọng chết để systemd restart toàn runtime.

## Layers

- Domain không phụ thuộc DeepStream, HTTP hoặc persistence implementation.
- Application điều phối typed samples/results/transitions và stale ordering gate.
- DeepStream adapter sở hữu graph, probes, tensor metadata, OSD và encode.
- Persistence adapter sở hữu event journal, SQLite idempotency và evidence files.
- Notification adapter chỉ nhận event đã có evidence.
- Dashboard đọc bounded status/event projections; không chạy inference.

## Configuration

Hai YAML profile standalone. Production không kế thừa development. Environment paths được resolve qua một runtime-path owner; source URL, function flags và model policy vẫn được validate trước khi tạo process.

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

Liveness chỉ chứng minh service HTTP sống. Readiness của vision camera yêu cầu process và output freshness; media-only feed giữ contract playback hiện hữu. Production acceptance bổ sung service state, release manifest, topology, media contracts, restart persistence và browser verification.
