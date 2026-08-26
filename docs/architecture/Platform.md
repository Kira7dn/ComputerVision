# LS-Vision Platform architecture

Ngày cập nhật: 26/08/2026

## Source of truth và ownership

`apps/` là runtime Camera AI canonical. Native Jetson là production target hiện hành. `services/camera-server/` là boundary ADAS/FTP/archive độc lập và không tham gia startup path của LS-Vision.

| Lane | Owner |
| --- | --- |
| Camera process lifecycle | `runner` |
| Mock group clock và synchronized publisher | Mock timeline runtime |
| Capture, DeepStream graph, tracking, annotation, RTSP | DeepStream adapter của từng camera |
| Face, DMS, smoking, fire/smoke, front inference | Model adapters được gán theo camera config |
| Event transition | Domain/application services của camera process |
| Evidence và idempotency | Persistence adapter |
| Notification | Durable outbox và notification adapters |
| Dashboard/read API | Interfaces layer |
| RTSP/HLS/WebRTC | Native MediaMTX |
| Release/start/restart/rollback | Camera deploy scripts và systemd |

Không component nào ngoài camera process được giữ tracking hoặc function decision state của camera đó. Notification không được tạo event; dashboard không được mutate runtime state.

## Camera topology

Hai profile giữ cùng thứ tự: `DMS`, `camera_front`, `camera_back`, `camera_left`, `camera_right`.

- DMS dùng Dahua channel 5 và function DMS.
- Camera front sở hữu front-assistance theo config hiện hành.
- Feed `media_only` chỉ phục vụ playback và không tạo vision worker.
- Bốn mock 360 dùng một contract `vehicle_surround`; timeline runtime chỉ publish `camera_front_raw`, ba feed còn lại được browser đọc trực tiếp và bù theo live latency của front.
- Controller timeline có thể restart và adopt synchronized publisher đang sống; full service shutdown vẫn dọn publisher theo systemd/dev process group.

Việc đổi topology, model, confirmation policy hoặc media-only behavior là product change riêng, không thuộc architecture cleanup.

## Event/evidence flow

```text
frame + lineage
  -> function result
  -> temporal transition
  -> evidence commit/idempotency
  -> event journal projection
  -> notification outbox
  -> dashboard/API
```

Evidence phải giữ camera, run ID, worker epoch, frame number, classification, bbox/score và immutable artifact references. Notification chỉ được enqueue sau evidence commit.

## Production lifecycle

Source được đóng gói thành versioned release kèm commit/config/model hashes. `current` symlink đổi atomically. Deployment chỉ được accepted sau native service/API/media/browser gates. Rollback quay về release trước và giữ nguyên runtime data.

## Open production gates

- Model inventory phải có checksum/provenance đầy đủ.
- Camera/model/GPU provider thật cần acceptance riêng.
- Auth/TLS, retention, disk-full, backup và log rotation cần operational gate.
- Browser WebRTC/HLS và notification provider cần artifact từ target thật.
