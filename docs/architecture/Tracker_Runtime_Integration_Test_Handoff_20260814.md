# Tracker Runtime Integration Test Handoff — 2026-08-14

## Kết luận hiện tại

Tracker extension và integration test **chưa được xác nhận pass**. Không được dùng các lần chạy hiện tại làm bằng chứng hoàn thành Phase 8 hoặc E2E.

Công việc bị dừng theo yêu cầu của chủ dự án. Tài liệu này bàn giao trạng thái thực tế để người tiếp quản không phải tái hiện lại toàn bộ quá trình.

## Yêu cầu cần hoàn thành

Integration test của tracker extension phải:

- chạy nhanh trên host, không Docker, không build image và không gọi launcher;
- chỉ kiểm tra tracker extension, nhưng đi qua khoảng 90% đường chạy production;
- dùng đúng video mock face và LPR làm đầu vào;
- đi qua chuỗi thật: capture FFmpeg → detector → native tracker → `TrackerRuntime` → journal → `TrackerService` gRPC/mTLS → `TrackerMaintainer` → `EventProcessor` → SQLite/media;
- không tự decode video bằng OpenCV;
- không gọi trực tiếp `CameraTrackAdapter.process`;
- không tự viết client gRPC thay cho `TrackerMaintainer`;
- không gọi trực tiếp `EventProcessor._process_event_update`;
- không tự bơm ACK, tự tạo END hoặc mock media reader;
- kỳ vọng 4 face trace IDs và 8–11 LPR trace IDs;
- mỗi trace có lifecycle `START → UPDATE* → END`;
- journal kết thúc với `pending=0`;
- Event, SQLite và media phải nhất quán, media phải đọc được và kiểm tra được hash.

## Phạm vi kiến trúc phải giữ

- Edge tracker sở hữu camera, capture, detection, tracking, media, PTZ và ONVIF.
- Frigate main chỉ validate/ingest và vẫn là chủ sở hữu canonical của Event, API, SQLite và publication.
- `frigate/src/extension/tracker` phải là wrapper mỏng, tận dụng tracking core tại `frigate/src/frigate/domain/track`.
- Không thay đổi recognition E2E hoặc contract của recognition để làm tracker pass.

## Trạng thái working tree cần kiểm tra trước khi làm tiếp

Các thay đổi tracker có chủ đích đang nằm tại:

- `frigate/src/extension/tracker/runtime.py`
- `frigate/src/extension/tracker/transport.py`
- `frigate/tests/test_tracker_edge.py`
- `frigate/tests/test_tracker_runtime_integration.py`

`test_tracker_runtime_integration.py` hiện là harness thử nghiệm quá lớn, khoảng 500 dòng. Nó chứa nhiều monkeypatch dành cho Windows và **không nên được xem là thiết kế cuối cùng**.

Các file Frigate main sau từng bị chạm trong quá trình xử lý:

- `frigate/src/frigate/const.py`
- `frigate/src/frigate/domain/camera/maintainer.py`
- `frigate/src/frigate/infrastructure/comms/config_updater.py`
- `frigate/src/frigate/infrastructure/comms/inter_process.py`
- `frigate/src/frigate/infrastructure/comms/object_detector_signaler.py`
- `frigate/src/frigate/infrastructure/comms/zmq_proxy.py`
- `frigate/src/frigate/util/config.py`
- `frigate/src/frigate/util/process.py`

So sánh trước đó bằng `git diff --ignore-space-at-eol` không thấy khác biệt nội dung với HEAD; trạng thái modified có khả năng liên quan LF/CRLF hoặc metadata. Người tiếp quản phải kiểm tra lại, không được blind-revert vì working tree có thể chứa thay đổi của chủ dự án.

## Những thay đổi production đáng giữ để review

### `runtime.py`

- bổ sung materialize media khi nhận lifecycle END;
- sử dụng `MediaManifest` và `EdgeMediaStore`;
- truyền `media_dir` vào `TrackerRuntime`.

### `transport.py`

- bổ sung đường `FetchMedia` qua gRPC;
- main có thể fetch edge media;
- reconstruct active lifecycle sau reconnect;
- sửa lỗi gRPC thật: không trộn `call.read()` với `async for`; vòng đọc hiện dùng `await call.read()` cho tới `aio.EOF`.

Các thay đổi này chưa có đủ regression evidence sau lần chỉnh cuối và phải được review độc lập.

## Bằng chứng chạy test gần nhất

Không có lần chạy integration tracker nào pass.

Các lỗi đã quan sát theo thứ tự:

1. Import FFmpeg sai từ module camera; harness đã đổi sang import module trực tiếp.
2. Config validation fail vì camera bật face recognition trong khi global face recognition bị tắt; harness đã giữ global enable.
3. Worker khởi động và ONNX model load được nhưng FFmpeg không chuyển frame thành công.
4. Diagnostic xác định lỗi Windows shared memory:
   `ValueError: memoryview assignment: lvalue and rvalue have different structures`.
   Buffer named shared memory có 1,384,448 byte trong khi frame YUV cần 1,382,400 byte.
5. Monkeypatch cắt memoryview tránh lỗi trên lại gây:
   `BufferError: cannot close exported pointers exist`.
6. Sau khi tiếp tục monkeypatch close, test vẫn timeout sau khoảng 105 giây vì không đạt 4 face starts và 8 LPR starts.

Log còn giữ tại:

- `.tmp/pytest-tracker-runtime-integration.log`
- `.tmp/pytest-tracker-runtime-probe.log`

Hai file này là bằng chứng fail/diagnostic, không phải artifact pass.

## Nghi vấn lỗi cần phân loại

### Lỗi chắc chắn của harness Windows

Production runtime dùng giả định IPC/shared-memory theo Linux. Việc vá bằng `sitecustomize.py` trong subprocess làm test phức tạp, dễ sai lifecycle và không còn là phép mô phỏng production đáng tin cậy.

### Lỗi có khả năng thuộc tracker production

Trong cleanup có lỗi:

`sqlite3.ProgrammingError: Cannot operate on a closed database`

`TrackerService.connect()` còn chạy `journal.replay()` sau khi `TrackerRuntime.stop()` đã đóng journal. Cần kiểm tra thứ tự shutdown: dừng gRPC server/connection trước, rồi mới đóng runtime/journal. Đây là lỗi thứ cấp được quan sát; chưa chứng minh nó là nguyên nhân khiến không có trace START.

### Lỗi production đã xác định và đã sửa nhưng chưa regression đầy đủ

Transport từng trộn hai API đọc stream của gRPC (`read()` và iterator), gây lỗi runtime. Thay đổi trong `transport.py` cần được giữ và có test trực tiếp.

## Điều không nên làm tiếp

- Không tiếp tục phình `test_tracker_runtime_integration.py` bằng monkeypatch mới.
- Không dùng Docker/E2E launcher để gọi đó là integration test nhanh.
- Không viết lại detector/tracker/video pipeline trong test.
- Không tạo trace hoặc lifecycle giả để đạt số lượng mong muốn.
- Không sửa Frigate main hay recognition chỉ để harness chạy được.
- Không báo pass chỉ vì process/service khởi động hoặc model load thành công.

## Hướng tiếp quản đề xuất

1. Kiểm tra và lưu diff hiện tại trước khi sửa.
2. Tách bỏ harness Windows thử nghiệm trong `test_tracker_runtime_integration.py`; giữ lại contract/assertion cần thiết.
3. Tái sử dụng helper không-Docker từ `tools/runtime/validate_platform_runtime.py` thay vì sao chép topology, TLS và lifecycle validation.
4. Ưu tiên chạy pytest host trong WSL/Linux để dùng đúng IPC/shared-memory production mà vẫn không cần Docker. Nếu bắt buộc chạy Windows Python, cần một platform adapter chính thức, nhỏ và có test riêng; không monkeypatch toàn runtime bằng `sitecustomize`.
5. Giữ file test orchestration ngắn, mục tiêu khoảng 100–150 dòng; helper dùng chung đặt ngoài test nếu thực sự tái sử dụng được.
6. Trước tiên chứng minh một camera đi trọn đường production, sau đó chạy đồng thời face và LPR và kiểm tra đủ số trace.
7. Chạy đúng một test node với verbose log; chỉ mở rộng khi node đó pass.
8. Sau integration pass mới chạy baseline/targeted tests theo `AGENTS.md`; sau đó mới tới healthy tracker E2E chính thức.

## Lệnh kiểm tra ban đầu cho người tiếp quản

Từ `D:\BusinessAnalyze\Camera`:

```powershell
git -C frigate status --short
git -C frigate diff -- src/extension/tracker tests/test_tracker_edge.py tests/test_tracker_runtime_integration.py
Get-Process python,pytest,ffmpeg -ErrorAction SilentlyContinue
```

Chạy riêng integration test, lưu đầy đủ log:

```powershell
$env:PYTHONPATH = "D:\BusinessAnalyze\Camera\frigate\src"
D:\BusinessAnalyze\Camera\.venv\Scripts\python.exe -u -m pytest -vv -s --capture=tee-sys -o log_cli=true -o log_cli_level=DEBUG frigate\tests\test_tracker_runtime_integration.py 2>&1 | Tee-Object .tmp\pytest-tracker-runtime-integration.log
```

Sau khi integration node pass, thực hiện các gate theo đúng thứ tự trong `AGENTS.md`; không dùng số test collect làm bằng chứng pass.

## Acceptance checklist

- [ ] Không Docker/build/launcher trong integration test.
- [ ] Dùng đúng hai mock video face và LPR.
- [ ] Capture, detector và native tracker chạy thật bên trong runtime path.
- [ ] Đi qua gRPC/mTLS bằng `TrackerMaintainer`.
- [ ] 4 face trace IDs.
- [ ] 8–11 LPR trace IDs.
- [ ] Mỗi trace có START/UPDATE/END hợp lệ.
- [ ] Journal pending bằng 0.
- [ ] Event và SQLite readback nhất quán.
- [ ] Media có thể fetch/read và hash đúng.
- [ ] Tracker targeted tests pass.
- [ ] Upstream baseline pass.
- [ ] Recognition E2E không bị regression.
- [ ] Healthy tracker E2E chính thức pass.

## Trạng thái bàn giao

**BLOCKED / NOT PASSING.** Chưa có bằng chứng tracker đã nhận đủ mock video và tạo đúng số trace IDs qua toàn bộ production-equivalent path.
