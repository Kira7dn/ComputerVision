# Biên bản bàn giao Phase 8 — Tracker Edge Node

Ngày chốt trạng thái: **2026-08-13**  
Workspace: `D:\BusinessAnalyze\Camera`  
Nested repository: `D:\BusinessAnalyze\Camera\frigate`

## 1. Quyết định kiến trúc phải được giữ nguyên

Production topology đã khóa là:

```text
Camera → camera-tracker → Frigate main → camera-recognition
                                      → API/publication/notification → ngrok
```

- Với camera được gán cho edge, `camera-tracker` sở hữu việc chạy capture/decode,
  detector, tracking, lifecycle, evidence, recording/live/media và PTZ/ONVIF.
- Frigate main giữ canonical Event/API/SQLite/notification/publication SOT, validate và
  idempotent-ingest update từ tracker, route recognition job và proxy media.
- Tracker không được gọi trực tiếp `camera-recognition`.
- Frigate main không được chạy lại camera pipeline cho camera đã gán edge và không được tự
  fallback sang embedded khi tracker lỗi.
- Edge runtime phải wrapper và tái sử dụng implementation Frigate hiện hành. Được tạo
  process/thread/queue/transport adapter mới, nhưng không được tạo behavior core thứ hai cho
  capture, detector, Norfair, `CameraState`, `TrackedObject`, zone/speed/path, candidate,
  recorder hoặc PTZ.
- Mọi build, healthy run, fault run và restore phải đi qua `deploy/run.ps1`.

`docs/architecture/Platform.md` là specification kiến trúc. `AGENTS.md` là hướng dẫn thao tác
và test, không có quyền thay đổi specification. Hiện tại cần đặc biệt rà lại câu trong
`AGENTS.md` nói Frigate sở hữu tracker/media vì câu đó xung đột với ownership Phase 8 nêu trên.
Không được dùng xung đột tài liệu này để âm thầm đổi specification.

## 2. Trạng thái source tại thời điểm bàn giao

### 2.1 Đã có trong working tree

- Tracker config đã được chuyển sang map node trực tiếp, không có tầng `nodes`:

  ```yaml
  tracker:
    edge-local:
      endpoint: tracker-edge-local:50052
      cameras: [face_camera, car_camera]
  ```

- Có central topology compiler tại:
  - `frigate/src/extension/topology/compiler.py`
  - `tools/runtime/compile_platform_topology.py`
- Topology plan hiện chịu trách nhiệm resolve recognition mode, tracker nodes, camera
  ownership, embedded camera view, per-node camera view, config revision và topology hash.
- `CameraMaintainer` đã được chỉnh theo hướng nhận camera view đã lọc thay vì tự quyết định
  topology.
- Frigate app đã có `TrackerMaintainer` và host-side imports dưới
  `frigate.track.edge.adapters`.
- External tracker source đã được gom từ package song song `frigate/tracker` sang
  `frigate/track/edge`:
  - `adapters/`
  - `runtime/`
  - `service/`
  - protobuf `camera.tracker.v1`
- Shared tracker lifecycle/policy đang nằm tại:
  - `frigate/src/frigate/domain/track/lifecycle.py`
  - `frigate/src/frigate/domain/track/policy.py`
- Launcher và tracker entrypoint hiện tham chiếu
  `python3 -u -m frigate.track.edge.service.app`.
- API/media/notification source có các thay đổi để resolve edge media qua
  `tracker_maintainer`.
- Healthy/fault tracker E2E entrypoint và common validator có thay đổi trong working tree,
  nhưng trạng thái pass chưa được chứng minh.
- Kế hoạch triển khai theo file hiện nằm tại
  `docs/architecture/Platform-Phase-8-Implementation-Plan.md`.

### 2.2 Chưa thực hiện

Đề xuất clean physical layout sau **chưa được áp dụng**:

```text
frigate/
  src/
    frigate/                    # upstream Frigate package
    extension/            # topology + recognition + tracker extensions
      config/
      topology/
      recognition/
      tracker/
  tests/
```

Không có file/package nào đã được chuyển sang `frigate/src` hoặc
`frigate/src/extension` trong lượt restructure cuối. Vì vậy không được giả định rằng
pyproject, Dockerfile, launcher overlay, module entrypoint hoặc test import đã hỗ trợ `src`
layout.

Các hạng mục chức năng Phase 8 quan trọng vẫn chưa có bằng chứng hoàn tất:

- Shared producer core thực sự được cả embedded và edge gọi chung.
- Differential fixture chạy cùng detector/frame sequence qua hai adapter.
- Full camera process wrapper sử dụng capture/detector/Norfair/recorder/runtime/output/PTZ hiện hữu.
- Canonical Event transaction và ACK-after-commit hoàn chỉnh.
- Recognition routing từ edge evidence qua Phase 7 adapter hiện hữu.
- Durable journal/evidence/media ownership chạy thật trong container.
- Managed multi-node services, TLS readiness và camera readiness qua launcher.
- Healthy chain tracker + Frigate + recognition + publication/API/media/ngrok.
- Fault scenarios, terminal cleanup và topology restore.

## 3. Trạng thái kiểm thử và acceptance

Tại điểm bàn giao này **không có kết quả test mới** cho refactor cuối:

- Không chạy pytest sau khi gom `tracker` vào `track/edge`.
- Không chạy `compileall`, Ruff hoặc `git diff --check` cho toàn bộ trạng thái hiện tại.
- Không chạy launcher build.
- Không chạy healthy Docker E2E.
- Không chạy fault E2E.
- Không có `report.md` mới chứng minh full production topology pass.

Do đó Phase 8 không được đánh dấu `[DONE]`. Những unit/build/runtime result cũ, nếu có, không
được dùng làm bằng chứng cho working tree hiện tại nếu source/worktree hash không khớp.

Theo yêu cầu gần nhất, công việc đã dừng trước test/build; biên bản này không tự tạo hoặc sửa
artifact để biến trạng thái thành pass.

## 4. Working tree cần được bảo toàn

Outer repository đang có thay đổi ở các nhóm sau:

- `AGENTS.md`
- `deploy/run.ps1`, `deploy/reference/tracker-run`
- `docs/architecture/Platform.md`
- tracker healthy/fault E2E, validator và launcher unit tests
- `tools/runtime/compile_platform_topology.py` mới
- ba file `runtime/db/test.db`, `runtime/db/test.db-shm`, `runtime/db/test.db-wal` đang hiện là deleted

Nested Frigate repository đang có nhiều file API/app/camera/events/notification/PTZ/test bị
modified; package cũ `frigate/tracker` hiện là deleted trong Git và source tương ứng nằm dưới
`frigate/track/edge` dưới dạng untracked. Đây là trạng thái move chưa staged, không phải bằng
chứng source đã bị mất. Không được reset/checkout/delete hàng loạt hoặc xóa untracked files.

Các thay đổi ngoài Phase 8 có thể cùng tồn tại trong working tree. Người tiếp nhận phải review
`git status`, `git diff` và `git -C frigate diff` theo từng file trước khi sửa hoặc phục hồi.

## 5. Rủi ro hiện tại

1. **Chưa chứng minh reuse:** edge scaffold có nhiều adapter/runtime class nhưng chưa chứng minh
   full call path dùng đúng camera pipeline Frigate hiện hữu.
2. **Package move chưa validate:** việc gom `frigate/tracker` vào `frigate/track/edge` đã đổi
   import/entrypoint nhưng chưa chạy targeted import/contract test.
3. **Specification conflict:** ownership sentence trong `AGENTS.md` có thể dẫn người tiếp nhận
   quay lại kiến trúc Frigate-owned tracker/media, trái Phase 8.
4. **Launcher chưa được chứng minh:** `run.ps1` và topology compiler thay đổi lớn nhưng chưa có
   launcher build/healthy artifact tương ứng.
5. **E2E có thể chỉ là scaffold:** entrypoint/validator thay đổi không đồng nghĩa full production
   chain đã chạy; phải kiểm tra report và trace do launcher sinh.
6. **Restructure toàn package có blast radius lớn:** di chuyển toàn bộ upstream Frigate sang
   `src` sẽ tác động pyproject, Docker COPY, `PYTHONPATH`, migrations, web assets, test fixture,
   launcher overlay và module entrypoint. Không nên thực hiện như một move cơ học duy nhất nếu
   chưa khóa compatibility boundary.

## 6. Hướng tiếp tục khuyến nghị

### A. Chốt source boundary trước khi restructure vật lý

Giữ import namespace upstream là `frigate.*`. Tách extension thành
`extension.{config,topology,recognition,tracker}` chỉ khi đã lập mapping đầy đủ cho:

- Python packaging/import root.
- Docker/runtime COPY và `PYTHONPATH`.
- Recognition/tracker module entrypoint.
- Launcher source overlay.
- migrations/assets/config và test fixtures.

Không đổi behavior API của upstream chỉ để đạt folder đẹp.

### B. Thực hiện theo lát dọc có thể phục hồi

1. Chụp inventory và diff của cả outer/nested repository.
2. Hoàn tất và review central topology/config boundary.
3. Chuyển riêng platform extensions sang `extension` và sửa import/entrypoint.
4. Chạy targeted import/config tests; nếu fail thì sửa ngay tại lát đó.
5. Chỉ sau khi extension boundary ổn mới cân nhắc chuyển upstream package sang `src/frigate`.
6. Sau source gate, chạy đúng thứ tự: targeted/unit → compile/Ruff/diff → launcher build →
   healthy E2E → fault E2E → restore.

### C. Acceptance bắt buộc

Healthy report phải được tạo tại:

```text
.tmp/platform-runtime/<run-id>/report.md
```

Report phải truy vết được source/worktree/config/topology/image hash và chứng minh full chain
`camera-tracker + Frigate + camera-recognition + publication/API/media + ngrok`, zero local
camera process cho assigned camera, no direct tracker-to-recognition traffic, ordered/idempotent
replay, terminal queues/ACK/evidence/journal về zero và restore thành công.

## 7. Kết luận bàn giao

Phase 8 hiện là **source đang phát triển, chưa được acceptance**. Không thể kết luận một tỷ lệ
chính xác là “vứt đi” chỉ từ diff: phần contract, topology, journal, service và tests có thể tái
sử dụng, nhưng phải audit call path để phân loại `REUSE`, `ADAPTER` hoặc `REPLACE/REMOVE`.

Điểm dừng an toàn là: giữ nguyên working tree, không tiếp tục restructure, không build/test từ
trạng thái chưa được người tiếp nhận review, và không thay đổi specification ownership đã khóa.
