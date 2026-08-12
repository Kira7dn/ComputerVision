# Phase 8 — Kế hoạch triển khai Tracker Edge Node theo file

## Config topology

`tracker` là map trực tiếp các external node; không có tầng `runtime`, `nodes` hoặc `cameras`
trung gian. Mỗi node sở hữu endpoint và danh sách camera riêng:

```yaml
tracker:
  edge-local:
    managed: true
    endpoint: tracker-edge-local:50052
    cameras: [face_camera, car_camera]
    tls:
      ca: /run/tracker-tls/ca.crt
      certificate: /run/tracker-tls/client.crt
      key: /run/tracker-tls/client.key
      server_name: tracker-edge-local
```

`frigate/src/camera_platform/topology/compiler.py` parse cả `recognition` và mọi entry trực tiếp dưới `tracker`
thành một immutable `PlatformTopologyPlan`; đây là nơi duy nhất quyết định runtime mode, ownership,
local camera view, per-node camera view, service name, endpoint và topology hash. `deploy/run.ps1` chỉ gọi
`tools/runtime/compile_platform_topology.py`, đọc manifest và triển khai service/config đã sinh.
Camera không nằm trong node nào giữ embedded path trong mixed rollout.

Apply contract:

1. Source config được validate và compile thành một revision/hash.
2. Compiler sinh `config.main.yml`, từng `config.tracker.<node-id>.yml` và
   `platform-topology.json` từ cùng plan; mỗi runtime view mang `topology_revision`,
   `topology_role` và `topology_node_id`.
3. `FrigateApp` tạo plan một lần; `TrackerMaintainer` duy trì client/node theo plan và mọi local
   component nhận embedded camera view đã resolve.
4. `TrackerNodeRuntime` chỉ nhận đúng tracker view của node; view chứa camera/node khác phải fail.
5. `CameraMaintainer` chỉ start/stop process trong immutable camera view được giao; add camera ngoài
   view yêu cầu topology restart, không được local fallback.

> Tài liệu này hướng dẫn hiện thực specification Phase 8 đã khóa trong
> `docs/architecture/Platform.md`. Nếu có xung đột, `Platform.md` là nguồn quyết định;
> tài liệu này không được dùng để thay đổi topology, ownership, contract hoặc hard gate.

Tài liệu này chỉ khóa cách hiện thực Phase 8 bằng code hiện hữu; không thay đổi topology, ownership,
contract hoặc hard gate trong `Platform.md`. Runtime tracker được phép tạo thread/process/queue và transport
adapter mới, nhưng mọi quyết định detection/tracking/lifecycle/media/PTZ/recognition phải gọi lại
implementation Frigate hiện hành. Mẫu áp dụng giống recognition: một behavior core, nhiều runtime
adapter; không có hai behavior core.

| Bước | File sửa/tạo | Thay đổi bắt buộc | Không được làm |
| --- | --- | --- | --- |
| **8-1A — Shared lifecycle core** | `frigate/src/frigate/domain/track/object_processing.py`, `frigate/src/frigate/domain/track/policy.py`, `frigate/src/frigate/domain/track/lifecycle.py` | Tách projection và quyết định dùng chung cho `START/UPDATE/END`, snapshot/clip retention, score/zones/path/speed và producer Event ID thành hàm/class không phụ thuộc transport. `TrackedObjectProcessor` tiếp tục gọi core này với Event/MQTT/local-media sink hiện hữu. | Không đổi output embedded; không copy callback hiện tại sang edge rồi sửa riêng; không đổi model, threshold, bbox, zone, speed hoặc lifecycle rule. |
| **8-1B — Edge lifecycle adapter** | `frigate/src/camera_platform/tracker/runtime/processor.py`, `frigate/src/camera_platform/tracker/runtime/producer.py` | Giữ `EdgeTrackedObjectProcessor` là wrapper quanh `CameraState` và shared lifecycle core. Adapter chỉ bind node/camera/epoch/sequence, evidence, journal và typed envelope. `TrackerProducerCore` là edge transport serializer, không phải behavior core dùng giả lập cho embedded. | Không tự tính lại `TrackedObject`, candidate, snapshot/clip policy hoặc PTZ decision trong adapter. |
| **8-1C — Differential fixture** | `frigate/tests/test_tracker_edge.py`, tạo `frigate/tests/fixtures/tracker_edge/` | Thay test gọi cùng `TrackerProducerCore` hai lần bằng replay cùng frame/detector sequence qua embedded adapter và edge adapter; so producer Event ID mapping, raw track ID, score history, bbox, zones, path/speed, candidate/evidence và đủ `START/UPDATE/END`. | Không mock output cuối rồi gọi đó là parity; không dùng raw OCR/face result làm ground truth. |
| **8-2A — Camera process wrapper** | `frigate/src/camera_platform/tracker/runtime/node.py`, `frigate/src/frigate/domain/camera/maintainer.py`, `frigate/src/frigate/video.py` | `TrackerNodeRuntime` tiếp tục tạo thread/process mới nhưng phải khởi tạo lại đúng `CameraMaintainer`, `CameraCapture`, `CameraTracker`, `ObjectDetectProcess` và queue/SHM contract hiện hữu. Chỉ thêm dependency injection/output sink cần cho edge. | Không tạo capture loop, FFmpeg builder, detector runner hoặc Norfair tracker thứ hai. |
| **8-2B — Recording/live/media wrapper** | `frigate/src/frigate/domain/record/record.py`, `frigate/src/frigate/runtime/output/output.py`, `frigate/src/camera_platform/tracker/runtime/node.py`, `frigate/src/camera_platform/tracker/runtime/media.py`, `frigate/src/camera_platform/tracker/adapters/media.py` | Cho edge khởi tạo và quản lý `RecordProcess`/`OutputProcess` hiện hữu trên volume node; adapter theo dõi output thật để đăng ký `MediaManifest`, SHA-256, expiry và range source. go2rtc dùng config camera đã lọc do launcher sinh. | Không viết recorder/clip/snapshot encoder mới; không tạo public media server tại edge; không đăng manifest nếu file thật chưa durable. |
| **8-2C — PTZ wrapper** | `frigate/src/frigate/domain/ptz/autotrack.py`, `frigate/src/frigate/domain/ptz/onvif.py`, `frigate/src/camera_platform/tracker/runtime/node.py`, `frigate/src/camera_platform/tracker/service/app.py` | Dùng nguyên `PtzMotionEstimator`, `PtzAutoTrackerThread` và `OnvifController`; inject calibration-result sink để embedded giữ behavior hiện tại còn edge phát typed config patch. Hoàn thiện control enable/disable, manual PTZ, preset, calibration và topology drain. | Không mở ONVIF thứ hai từ main; edge không ghi trực tiếp config mount; không thay thuật toán motion compensation/autotracking. |
| **8-3A — Main tracker maintainer** | `frigate/src/camera_platform/tracker/adapters/frigate.py`, `frigate/src/frigate/app.py`, `frigate/src/camera_platform/tracker/service/grpc_client.py`, `frigate/src/camera_platform/tracker/adapters/ingest.py` | Frigate main tạo đúng một client/node, reconnect theo deadline, validate ownership/epoch/sequence và replay từ durable ACK. `TrackerMaintainer` chuyển update đã validate sang existing Event và recognition sinks; start/stop/drain cùng `FrigateApp`. | Không để client chỉ tồn tại trong unit test; không ACK trước canonical transaction; không fallback sang embedded. |
| **8-3B — Event transaction và idempotency** | `frigate/src/frigate/application/events/maintainer.py`, `frigate/src/frigate/application/events/canonical.py`, `frigate/src/camera_platform/tracker/adapters/canonical.py`, `frigate/src/frigate/models.py`, `frigate/migrations/040_create_tracker_edge_ingest.py` | Giữ `EventProcessor`/`EventAggregator` là writer canonical duy nhất. Edge canonical adapter chỉ làm ingest ledger/media-manifest transaction và completion receipt; Event lifecycle phải đi qua existing Event queue/path với producer Event ID. ACK journal chỉ phát sau Event/SQLite và manifest acceptance cùng thành công. | Không direct-write hoặc tạo Event store thứ hai; không dùng raw track ID thay producer Event ID; không tạo synthetic end/media khi transport lỗi. |
| **8-3C — Recognition routing** | `frigate/src/camera_platform/tracker/adapters/frigate.py`, `frigate/src/frigate/application/embeddings/maintainer.py`, `frigate/src/frigate/infrastructure/data_processing/real_time/external_recognition.py` | Host lấy đúng I420 evidence theo reference, materialize thành bounded one-shot frame source rồi đưa vào cùng frame-processing/`ExternalRecognitionProcessor` Phase 7. Chỉ bổ sung source adapter và lineage fields nếu existing interface chưa nhận edge frame. | Không tạo recognition client/core/session thứ hai; không sửa Face/LPR algorithm; không tracker-to-recognition connection. |
| **8-4A — Main camera ownership** | `frigate/src/camera_platform/topology/compiler.py`, `frigate/src/frigate/app.py`, `frigate/src/frigate/domain/camera/runtime.py`, `frigate/src/frigate/domain/camera/maintainer.py`, `frigate/src/frigate/domain/track/object_processing.py`, `frigate/src/frigate/domain/record/record.py`, `frigate/src/frigate/runtime/output/output.py`, `frigate/src/frigate/domain/ptz/autotrack.py`, `frigate/src/frigate/domain/ptz/onvif.py` | Compile ownership đúng một lần thành immutable topology plan. Main và từng edge node nhận camera config view đã resolve trước khi tạo process; `CameraMaintainer` chỉ quản lý process trong view được giao và không tự diễn giải topology. Main vẫn giữ full camera config cho API/Event; camera unassigned giữ embedded path. | Không xóa camera edge khỏi canonical config; không tự fallback; không để PowerShell hoặc từng component tự tính ownership riêng. |
| **8-4B — Media/API proxy** | `frigate/src/camera_platform/tracker/adapters/media.py`; sửa `frigate/src/frigate/api/media.py`, `frigate/src/frigate/api/media_auth.py`, `frigate/src/frigate/application/notifications/client.py`, `frigate/src/frigate/application/notifications/media.py` | Resolver tra `EdgeMediaManifest` theo Event, giữ nguyên auth/signature/API URL hiện hành và proxy byte range qua tracker client. Notification dùng cùng resolver/API, ngrok vẫn chỉ trỏ Frigate. | Không thêm public edge route; không bypass camera authorization; không copy toàn bộ edge media sang Frigate. |
| **8-5A — Config/contract completion** | `frigate/src/frigate/infrastructure/config/tracker.py`, `frigate/src/frigate/infrastructure/config/config.py`, `frigate/src/camera_platform/tracker/contracts.py`, `frigate/src/camera_platform/tracker/service/v1/tracker.proto`, generated `tracker_pb2*.py`, `frigate/src/camera_platform/tracker/service/wire.py` | Hoàn thiện filtered config revision/hash, typed config patch, control ACK, failure/gap/media manifest và compatibility fixture. Regenerate protobuf rồi check-in generated output. | Không đổi field đã khóa không có migration/compatibility test; production không plaintext hoặc filesystem path contract. |
| **8-5B — Launcher and managed services** | `frigate/src/camera_platform/topology/compiler.py`, `tools/runtime/compile_platform_topology.py`, `deploy/run.ps1`, `deploy/reference/Dockerfile.tracker`, `deploy/reference/tracker-run`, `deploy/reference/docker-compose.yml`, `deploy/config.yaml`, `deploy/README.md` | Shared compiler sinh immutable plan, main/per-node config và manifest; `run.ps1` chỉ consume manifest để build service/volume, start tracker trước main, bounded gRPC+camera readiness và ghi image/source/worktree/config/topology/epoch/queue/spool/media/restore artifact. Fault action cũng phải là launcher action. | Không lặp config parsing/filter/ownership trong PowerShell; không gọi Docker/Compose trực tiếp từ E2E; không coi process health là camera readiness; không sửa artifact lỗi thành pass. |
| **8-6A — Common healthy E2E** | Thu gọn `tools/tests/e2e/run_external_tracker_runtime_test.py`; mở rộng `tools/runtime/validate_platform_runtime.py`; sửa `tools/tests/unit/test_external_tracker_launcher.py`, `tools/tests/unit/test_passage_acceptance.py` | Tracker entrypoint chỉ parse tracker topology rồi gọi common validator như `run_external_recognition_runtime_test.py`. Validator dùng output duy nhất `.tmp/platform-runtime/<run-id>/`, chạy full chain tracker → main → recognition → publication/API/media/ngrok và kiểm tra ownership/cleanup/restore. | Không tạo validator, report schema hoặc output root thứ hai; không tự sinh PASS từ state.json chưa đủ field. |
| **8-6B — Fault E2E** | `tools/tests/e2e/run_external_tracker_fault_test.py`, `tools/runtime/validate_platform_runtime.py`, `deploy/run.ps1` | Fault wrapper truyền scenario vào common validator; validator yêu cầu launcher thực thi `tracker_restart`, `stream_disconnect`, `client_disconnect`, `spool_replay`, `media_unavailable`, rồi kiểm tra ordered replay, typed failure, publication safety và terminal zero. | Không gọi `docker restart`, `docker network disconnect` hoặc container mutation trực tiếp trong validator. |
| **8-7 — Documentation and acceptance evidence** | `docs/architecture/Platform-Test-Report.md`, `deploy/README.md`, `AGENTS.md`, mục trạng thái `15.8` của `docs/architecture/Platform.md` | Chỉ cập nhật trạng thái từ artifact thật sau đúng thứ tự unit → compile/Ruff/diff → launcher build → healthy → fault → restore. Ghi rõ source/unit/build/healthy/fault độc lập. | Không đổi specification Phase 8; không đánh dấu `[DONE]` từ scaffold, unit, build hoặc một healthy run riêng lẻ. |

Thứ tự source gate bắt buộc: hoàn tất **8-1A đến 8-1C** và embedded differential pass trước
**8-2**; full one-camera chain **8-2 đến 8-4** pass targeted test trước launcher build; chỉ sau
healthy **8-6A** mới chạy fault **8-6B** và canary camera thứ hai. File ngoài hàng đang thực hiện
không được sửa chỉ để mở gate, trừ fix portability nhỏ có targeted regression riêng.
