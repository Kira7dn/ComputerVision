# Tracker runtime handoff — 2026-08-13

## Kết luận hiện tại

Tracker không bị block ở việc đọc MP4 hay bind gRPC. Tracker đã nhận đúng hai source trực tiếp, khởi động detector và camera worker, nhưng E2E chưa sinh `trace_id` vì pipeline chưa đi qua đầy đủ chuỗi tracker → Frigate main → recognition → publication.

## Hiện tượng quan sát được

| Hiện tượng | Bằng chứng | Trạng thái |
| --- | --- | --- |
| Tracker lấy được MP4 | `container-inspect-tracker-edge-local.json` có mount `/runtime-input/face_camera.mp4` và `/runtime-input/car_camera.mp4` | Đã xác nhận |
| Tracker khởi động detector và worker | `container-tracker-edge-local.log`: ONNX loaded; camera processor/capture started cho cả hai camera | Đã xác nhận |
| Tracker gRPC hoạt động | Port `50052` LISTEN; `GetCapabilities` trả `node_id=edge-local`, mTLS và hai camera | Đã xác nhận |
| Recognition từng restart liên tục | `camera-recognition`: `ExitCode=1`, nhiều lần restart, không có health SERVING | Đã xác định nguyên nhân startup |
| Recognition sau đó đã listening | Artifact `20260813-233139-039/container-recognition.log` ghi `Recognition service listening on 0.0.0.0:50051` | Đã xác nhận sau sửa |
| E2E vẫn timeout | Artifact `20260813-233139-039/summary.json`: `acceptance-start ... timed out after 90 seconds` | Chưa hoàn tất |
| Không có `trace_id` | `media/runtime-trace.jsonl` không được tạo; report có 0 raw traces, 0 attempts, 0 publications | Hệ quả, chưa phải root cause độc lập |

## Nguyên nhân đã xác định

Recognition gọi `ArcFaceRecognizer.build()` trong lúc dựng `FrigateRecognitionModel`, trước khi mở gRPC health endpoint. Với face library của fixture, build worker bị kẹt trong startup và container thoát `ExitCode=1`; `Wait-RecognitionReady` vì vậy không bao giờ pass.

Đã sửa `frigate/src/extension/recognition/models.py` để face-library build lazy, không chặn service startup. Targeted test recognition pass `8/8`; build runtime pass.

## Nghi vấn còn mở

1. `acceptance-start` vẫn timeout sau khi recognition và tracker đều listening. Cần xác định chính xác nó dừng ở `Wait-TrackerReady`, `docker compose up frigate`, hay readiness của Frigate main.
2. Frigate main có thời điểm ở `health: starting`; log có lỗi upstream `mediamtx` khi container đang dùng config replay cũ sau restore. Cần đối chiếu config/hash của chính container trong lúc acceptance, không dùng log sau restore để kết luận.
3. Tracker camera health lúc startup có thể chưa có FPS dù process đã tồn tại. Vì vậy `acceptance-start` đã được đổi sang service-only readiness; camera/frame readiness phải do acceptance runner kiểm tra sau khi Frigate main attach source.
4. Cần kiểm tra lại việc `config.main.yml`, compose override và topology direct source có đồng nhất trong cùng một run hay không. Một số log sau restore phản ánh config replay, không phải config direct của run lỗi.

## Thay đổi đã thực hiện

- Lazy face-library build trong recognition model.
- `acceptance-start` gọi `Wait-TrackerReady ... -RequireCameras:$false`.
- Giữ camera readiness trong validator sau khi Frigate main được start.
- Bổ sung publisher live từ tracker processor vào gRPC service.
- Giữ bounded queue và journal làm nguồn replay cho update bị stale-drop.

## Bằng chứng kiểm thử

- `frigate/tests/test_external_recognition_runtime.py`: **8 passed**.
- `tools/tests/unit/test_external_tracker_launcher.py`: **8 passed** sau khi cập nhật readiness contract.
- `deploy/run.ps1 build`: **pass**, tạo image mới cho Frigate, recognition và tracker.
- Healthy tracker E2E: **chưa pass**; các artifact lỗi được giữ nguyên:
  - `.tmp/platform-runtime/20260813-232543-057`
  - `.tmp/platform-runtime/20260813-233139-039`

## Việc cần làm tiếp

Chạy một lần acceptance có log launcher tách riêng từng bước và chụp ngay trước restore:

1. ghi timestamp trước/sau `Wait-RecognitionReady`;
2. ghi timestamp trước/sau `Wait-TrackerReady -RequireCameras:$false`;
3. ghi timestamp trước/sau `docker compose up frigate`;
4. lấy `docker inspect`, config hash và `docker logs frigate` trước khi restore;
5. chỉ sau khi Frigate main healthy mới đánh giá `trace_id` và tracker publication.

Không được báo tracker E2E pass cho tới khi artifact có `track_seen`, `trace_id`, recognition outcome và Event/API/SQLite readback.
