# Kiến trúc Camera AI B2B

Ngày cập nhật: 12/08/2026

## 1. Mục tiêu

Tài liệu này định nghĩa kiến trúc pipeline Computer Vision nhiều camera với các boundary rõ ràng:
`tracker` xử lý edge, `frigate` main là system of record và `camera-recognition` xử lý Face/LPR.
Tracker giao tiếp với Frigate main; không giao tiếp trực tiếp với recognition.

Mục tiêu kinh doanh ưu tiên là **Gate Intelligence cho nhà máy, kho vận, depot và
bãi xe đang có camera RTSP nhiều hãng**. Sản phẩm không cạnh tranh trực diện với
camera ANPR/face chuyên dụng bằng một model đơn lẻ; lợi thế chính là:

- Không bắt buộc thay toàn bộ camera hiện có.
- Dữ liệu và inference chạy on-premise.
- Kết quả nhận diện giữ đúng frame, bbox và evidence.
- Tích hợp được barrier, ERP, WMS, visitor management và hệ thống bảo vệ.
- Không khóa vào firmware hoặc cloud của một hãng camera.

## 2. Baseline hiện tại

Các thành phần đã có và được giữ làm nền tảng:

- Frigate hiện vẫn chạy toàn bộ pipeline trong một container; kiến trúc đích tách lane
  capture/detection/tracking thành `tracker` edge và giữ Event/publication trong Frigate main.
- `deploy/config.yaml` là cấu hình runtime hiện hành.
- Hai pipeline đang chạy là `car_camera` cho LPR và `face_camera` cho face recognition.

Tối ưu được thực hiện trực tiếp trong pipeline Frigate hiện có.

Trạng thái vận hành ngày 07/08/2026:

| Camera | Pipeline | Processing |
| --- | --- | --- |
| `car_camera` | Car detection + native LPR | Bật |
| `face_camera` | Person detection + face recognition | Bật |

### 2.1 Readiness snapshot ngày 08/08/2026

Các bằng chứng hiện có được phân loại như sau; `pass` unit test hoặc stream health không
được nâng thành production acceptance:

| Hạng mục | Bằng chứng hiện có | Trạng thái kiến trúc |
| --- | --- | --- |
| Hai-camera stream health | Replay giữ camera/process FPS ổn định | Diagnostic pass; không phải passage recognition acceptance |
| LPR hai camera | Baseline passage recall 60%; Phase 2 đã tăng lên 100% trên fixture replay | Passage bottleneck Phase 2 đã đạt; recognition tiếp tục được cải thiện |
| Recognition runtime boundary | Face/LPR đang chạy trong Frigate; boundary service đã có | Frigate main gửi job và nhận outcome từ `camera-recognition`; Event/media vẫn ở Frigate |

## 3. Khoảng trống cần giải quyết

| Khoảng trống | Hệ quả hiện tại | Trạng thái đích |
| --- | --- | --- |
| Camera chỉ được mô tả bằng stream/FPS | Không biết input có đủ chất lượng để cam kết hay không | Mỗi camera có quality threshold đã benchmark |
| Detect và evidence dùng chung luồng thấp | Mất pixel biển số/khuôn mặt | Detect stream thấp, evidence stream full-resolution |
| Face và LPR cần evidence đúng lineage | Sai frame/bbox làm report không đáng tin | Evidence/quality chỉ là side-channel, không thay recognition decision của master |
| Quan sát chủ yếu bằng FPS/inference | Không biết bỏ sót bao nhiêu passage | SLA theo passage và end-to-end funnel |
| Detection input gắn với subscriber nội bộ | Khó thay detector/tracker mà không sửa recognition | Một `RecognitionInput` port nhận `TrackedObservation` có lifecycle rõ |
| Trace ghi file ngay trên thread gọi | Trace I/O có thể chặn detection/recognition khi bật | Bounded trace queue và một trace-writer thread riêng |
| Detect, face và LPR cùng tranh chấp compute | LPR/OCR tạo burst CPU/GPU và giảm mật độ kênh | Queue/transport bounded nhưng giữ nguyên cadence và voting của master |

## 4. Nguyên tắc kiến trúc

1. Chất lượng camera input là contract có thể đo.
2. Candidate giữ đúng frame và bbox đã dùng để nhận diện.
3. Live/network input dùng latest-frame khi quá tải; finite local MP4 dùng FIFO/backpressure để
   không bỏ frame.
4. Face/LPR giữ nguyên admission, history, attempt limit và publish timing của Frigate `master`.
5. Recognition decision phải giữ đúng Frigate `master`; instrumentation/coordinator không được
   thay weighted voting, clustering, threshold hoặc thời điểm publish.
6. Refactor worker/coordinator chỉ được thực hiện sau khi master-parity đã có differential test và
   phải chứng minh không đổi decision output hoặc raw track/Event ownership.
7. `EventAggregator` tiếp tục là canonical Event writer; notification chỉ đi từ committed Event
   qua outbox.
8. Recognition chỉ phụ thuộc typed tracked-observation/evidence contract; detection, Event và
   transport cụ thể nằm trong adapter.
9. Trace là side-channel bounded, không phải source of truth và không được block critical path.
10. `tracker` chỉ gửi tracked-object result stream cho Frigate main. Frigate main là integration
    boundary duy nhất giữa tracker và recognition; tracker không gọi trực tiếp recognition.
11. Nhãn `Phase N` chỉ dùng trong roadmap/tài liệu/acceptance artifact; production code, config,
    public API, metric và test name dùng tên chức năng, không mang phase label.

### 4.1 Boundary deployment hiện hành

Runtime hỗ trợ cả camera do Frigate-contained tracker sở hữu và camera được gán cho edge tracker.
Topology Phase 8 đã kiểm chứng dùng `camera-tracker-edge-local` làm tracker owner, đồng thời giữ
Frigate main làm Event/publication SOT:
`tracker` phát detection/track result, giữ evidence và media bytes cho camera edge; `frigate` main
validate stream/manifest, route candidate/evidence, sở hữu Event/API/SQLite/notification/publication,
proxy media và gọi `camera-recognition`. `camera-recognition` chỉ xử lý Face/LPR và trả outcome.

## 5. Kiến trúc đích

Sơ đồ dưới đây là kiến trúc service chuẩn đã được kiểm chứng cho local edge topology. Camera có thể
được chuyển owner giữa Frigate-contained và edge tracker, nhưng việc chuyển producer của
tracked-object stream không thay Event SOT.

```mermaid
flowchart LR
    subgraph Tracker[tracker edge boundary]
        Camera[Camera ingest / FFmpeg / decode]
        Detect[Object detection]
        Track[Association / track lifecycle]
        EdgeMedia[Evidence ring / recording / live / clip]
    end

    subgraph Frigate[frigate main]
        Client[TrackedObject input + validation]
        Candidate[Candidate / crop / bbox / evidence]
        Guard[Epoch / sequence / idempotency guard]
        EventAdapter[FrigateEventAdapter]
        Event[EventAggregator]
        Media[Authenticated media proxy / trace writer]
        Database[API / SQLite / notification outbox]
    end

    subgraph Service[Recognition container]
        Grpc[gRPC mTLS server]
        Queue[Bounded executor]
        Models[Face / LPR model adapters]
        Core[RecognitionCore + session state]
        Control[Config + Face library control]
        Health[Health / capabilities / service_epoch]
    end

    Camera --> Detect --> Track --> Client
    Track --> EdgeMedia
    EdgeMedia -->|Evidence / MediaManifest / byte range| Client
    Client --> Candidate --> Guard
    Client -->|RecognitionJob + raw I420 evidence| Grpc
    Grpc -->|JobReceipt / RecognitionOutcome| Client
    Grpc --> Queue --> Models --> Core
    Core -->|RecognitionOutcome| Grpc
    Guard --> EventAdapter --> Event --> Database
    Guard --> Media
    Client -. configure / Face operations .-> Control
    Health -. readiness .-> Client
```

### 5.1 Runtime lanes và ownership

Ranh giới deployment được cố định như sau:

| Container/service | Ownership chính | Không được sở hữu |
| --- | --- | --- |
| `frigate` | Embedded camera chạy pipeline hiện hành; edge camera dùng host adapter. Luôn giữ EventAggregator, API/SQLite, notification/publication và authenticated media proxy | Không chạy lại camera logic hoặc giữ media bytes cho camera edge; không bypass Event SOT |
| `tracker` | Với camera được gán: ingest, go2rtc/FFmpeg, detection, Norfair/`TrackedObject`, zone/speed/path, PTZ, evidence, recording/live/clip và durable spool | Face/LPR decision, Event/API/canonical SQLite, notification/publication hoặc gọi trực tiếp `camera-recognition` |
| `camera-recognition` | Face/LPR candidate processing, crop/bbox/evidence artifact, model, `RecognitionCore`, history/session và outcome | Tracker, Event/API/SQLite, media, notification hoặc tự publish |
| `camera-mediamtx` | Replay/RTSP gateway | Recognition decision và Event commit |
| `camera-ngrok` | HTTP tunnel vào Frigate | Camera inference, recognition và database |

`camera-recognition` là **tập con recognition của Frigate về chức năng và code** (candidate,
crop/bbox/evidence processing, model, `RecognitionCore`, history/session), được đóng gói thành
deployment service riêng. Nó không phải một hệ thống nghiệp vụ độc lập và không bao gồm toàn bộ
Frigate. `tracker` là boundary edge riêng cho detection/tracking, nhưng chỉ giao tiếp với Frigate
main.
`camera-mediamtx` và `camera-ngrok` mới là infrastructure services bên ngoài Frigate core.

Ma trận capability theo ownership kiến trúc. `✅` nghĩa là service/boundary có hoặc sẽ sở hữu
capability; `—` nghĩa là không sở hữu capability đó. Cột `frigate` là runtime hiện tại;
`camera-recognition` là boundary có thể tách cho toàn bộ Face/LPR recognition; `tracker` là
boundary edge có thể tách cho ingest/detection/tracking. Hai boundary sau chưa phải Docker
container riêng đang chạy.

| Capability/tính năng | `frigate` | `camera-recognition` | `tracker` | `camera-ngrok` |
| --- | :---: | :---: | :---: | :---: |
| Camera ingest, FFmpeg và decode | ✅ | — | ✅ | — |
| Object detection | ✅ | — | ✅ | — |
| Track association và track lifecycle | ✅ | — | ✅ | — |
| Detection/Track result stream | ✅ | — | ✅ | — |
| Frame/track lineage (`camera_id`, `frame_seq`, `source_pts`, `edge_epoch`) | ✅ | — | ✅ | — |
| Face/LPR candidate | ✅ | ✅ | — | — |
| Face/LPR crop | ✅ | ✅ | — | — |
| Face/plate bbox | ✅ | ✅ | — | — |
| Face/LPR evidence | ✅ | ✅ | — | — |
| Recognition job admission, sequence và epoch guard | ✅ | — | — | — |
| Face/LPR model inference | ✅ | ✅ | — | — |
| Face/LPR history, voting và `RecognitionCore` | ✅ | ✅ | — | — |
| Recognition outcome | ✅ | ✅ | — | — |
| Recognition outcome: nhận/validate/publication | ✅ | — | — | — |
| Event metadata mapping và publication guard | ✅ | — | — | — |
| Canonical Event commit và correlation | ✅ | — | — | — |
| API và SQLite | ✅ | — | — | — |
| Media, recording và review | ✅ | — | — | — |
| Notification outbox/worker | ✅ | — | — | — |
| Bounded queue, backpressure và stale-drop | ✅ | — | ✅ | — |
| Health/readiness, reconnect và typed failure | ✅ | ✅ | ✅ | ✅ |
| gRPC/mTLS, node identity và certificate lifecycle | ✅ | ✅ | ✅ | — |
| Model/config/schema version và hash | ✅ | ✅ | ✅ | — |
| Runtime metrics, trace và resource telemetry | ✅ | ✅ | ✅ | ✅ |
| HTTP tunnel / public access | — | — | — | ✅ |

Chú thích: `frigate` là runtime đang chạy và hiện sở hữu toàn bộ capability của pipeline. Các dấu
`✅` trong cột `camera-recognition` và `tracker` mô tả ownership boundary khi tách service, không
khẳng định các container đó đang chạy. `camera-recognition` bao gồm candidate, crop, bbox, evidence,
model inference, history/voting và `RecognitionCore`; `frigate` vẫn giữ Event, API, SQLite, media,
notification và publication. `tracker` sẽ cung cấp ingest/detection/tracking/result stream; hiện
những capability đó vẫn nằm trong `frigate` cho đến khi edge runtime được triển khai và acceptance.
`camera-ngrok` chỉ cung cấp HTTP tunnel/public access, không tham gia camera processing,
recognition hoặc Event commit.

| Lane | Owner | Được phép làm | Không được làm |
| --- | --- | --- | --- |
| Detection | Capture/detect/track runtime | Phát tracked-object update, frame/evidence reference và lifecycle end | OCR/embed, recognition decision, notification hoặc disk trace |
| Frigate external client | `ExternalRecognitionClient` | Map canonical update sang ordered job; sở hữu evidence TTL; kiểm tra epoch/sequence/idempotency | Chạy model, vote, tự retry sang runtime khác hoặc publish result chưa hợp lệ |
| Recognition service | Executor + model adapters + `RecognitionCore` | Inference, Face/LPR history/voting, explicit end, Face library control; tạo producer-owned evidence artifacts trong outcome khi capture được yêu cầu | Import Event/SQLite/notification, ghi filesystem media hoặc tự tải URL/path evidence |
| Frigate output adapter | `FrigateEventAdapter` | Map outcome đã qua guard sang Event metadata và media contract hiện hành | Chọn lại winner, đổi score hoặc nối history qua service epoch |
| Event | `EventAggregator` | Canonical Event/API/SQLite commit và correlation | Chạy OCR/embed hoặc sở hữu recognition history |
| Notification | Durable outbox/worker | Gửi từ committed Event, retry/idempotency | Nhận lệnh trực tiếp từ recognition worker |
| Trace | Một bounded queue + writer thread | Persist JSONL/metrics best-effort | Block detection/recognition/Event hoặc sở hữu evidence |

Trong deployment hiện tại, detector/tracker trong `frigate` cập nhật base Event path. Trong kiến
trúc đích, `tracker` phát `track_start/update/end` cho Frigate main; Frigate là owner duy nhất của
Event, API/SQLite, media và publication, còn recognition service là owner của model, Face/LPR
history và decision state. Recognition không bao giờ nhận stream trực tiếp từ tracker.

### 5.2 Recognition input/output boundary

Frigate gửi một ordered job contract ổn định:

```python
RecognitionJob(
    job_id,
    client_id,
    expected_service_epoch,
    track_key,
    sequence,
    operation,          # observe | end_track | cancel
    observation,
    deadline_budget_ms,
    evidence,
)
```

`track_key = camera_id + stream_epoch + track_id` do Frigate cung cấp. `sequence` tăng đơn điệu
trong từng track; `end_track` đứng sau mọi observation đã được accept của track đó và idempotent.
Service không tự tracker, tạo passage identity hoặc suy diễn end từ observation bị thiếu.
Wire contract dùng budget tương đối; client giữ gRPC deadline bằng monotonic clock của chính nó,
service đổi budget còn lại thành monotonic deadline cục bộ khi nhận job. Không truyền giá trị
monotonic tuyệt đối giữa hai máy.

Service trả `JobReceipt` ngay khi admission và `RecognitionOutcome` sau xử lý. Receipt chỉ có
`accepted=true` hoặc typed reject như `queue_full`, `invalid_evidence`, `epoch_mismatch` và
`unavailable`. Outcome giữ job/key/sequence/epoch, frame/evidence lineage, `RecognitionUpdate`
hoặc typed failure. Client chỉ chuyển update sang `FrigateEventAdapter` một lần sau khi guard đạt.

### 5.3 Evidence và message contract

External V1 gửi raw contiguous I420 bytes cùng `shape`, `dtype`, `layout`, byte length, evidence ID
và expiry. Giới hạn hard là 8 MiB/job; length phải khớp shape/dtype trước khi model attach. Không JPEG
evidence trên inference path vì encode có thể đổi model input. Không chia sẻ `/dev/shm` hoặc IPC
namespace giữa hai container, không gửi URL/path và không để service đọc filesystem tùy ý.

Frigate giữ evidence đến khi nhận outcome/ack hoặc TTL hết. Service chỉ giữ input buffer trong phạm
vi job và không tự ghi filesystem media. Khi acceptance capture được yêu cầu, code producer dùng
chung tạo `recognition_attempt`, `recognition_attempt_bbox` và exact `face_crop`; service trả bytes,
hash và bbox metadata trong outcome để Frigate writer persist. Validator chỉ kiểm tra/copy artifact,
không vẽ lại bbox hay dựng record. Evidence contract không được lọc observation, xếp hạng candidate,
vote hoặc trì hoãn publication.

### 5.4 Deployment, control plane và no-fallback contract

```yaml
recognition:
  runtime: external
  endpoint: recognition:50051
  deadline: 5       # seconds; connect/RPC
  job_deadline: 30  # seconds; accepted observation: queue + inference
  tls:
    ca: /run/recognition-tls/ca.crt
    certificate: /run/recognition-tls/client.crt
    key: /run/recognition-tls/client.key
    server_name: recognition
```

Recognition image chạy thành service riêng trên private Docker network và không publish port ra
host mặc định. Model assets mount read-only; Face library là volume service sở hữu. Frigate gửi
validated recognition config qua control RPC và forward các operation `register`, `clear`,
`recognize` và `reprocess` Face hiện hành. Config change hoặc service restart tạo `service_epoch`
mới, đóng toàn bộ session/pending cũ và không nối history.

Chọn `external` thì Frigate không khởi tạo local Face/LPR inference model. Thiếu endpoint/TLS hoặc
config bị service từ chối làm startup validation fail. Sau startup, mất kết nối hoặc service
unhealthy chỉ làm recognition fail closed bằng typed outcome; capture/detect/base Event vẫn chạy,
không fallback sang local runtime, CPU hoặc model khác và không tự resubmit job.

Local và external không có hai implementation recognition khác nhau. Cả hai gọi chung
`RecognitionCore`, Face/LPR engine, Face detector/crop/bbox/evidence helpers và LPR processing
mixin. External chỉ thêm copy evidence, bounded admission, gRPC/mTLS và epoch/sequence guard.
Khi Frigate gửi `end_track`, track chuyển sang trạng thái `ending`; mọi observation đã accept và
xếp trước end vẫn được apply. Chỉ outcome `ENDED` mới đóng lineage và dọn sequence/media state.

### 5.5 Trace transport

Mọi lane chỉ `put_nowait()` một trace record nhỏ vào bounded trace queue. Trace không chứa image
bytes và không giữ evidence lease. Khi queue đầy, production drop record chưa persist và tăng
`trace_dropped_total`; không block critical path. Report phải ghi nguyên drop count để biết evidence
quan sát có đầy đủ hay không. Writer là thread duy nhất mở/ghi JSONL và shutdown chỉ flush trong
thời gian bounded.

### 5.6 Thiết kế boundary `tracker` edge

`tracker` là boundary edge được suy ra từ pipeline hiện tại của Frigate, không phải một tracker
mới với thuật toán mới. Mục tiêu là chuyển phần xử lý liên tục theo camera sang node edge để
scale số camera bằng compute tại chỗ. Frigate giữ Event/persistence/publication và proxy media;
tracker giữ media bytes/retention cho camera edge. Code tham chiếu hiện tại là `frigate/src/frigate/domain/video/detect.py`,
`frigate/src/frigate/domain/video/ffmpeg.py`, `frigate/src/frigate/domain/track/norfair_tracker.py`,
`frigate/src/frigate/domain/track/tracked_object.py` và `frigate/src/frigate/domain/track/object_processing.py`.

| Lane của tracker | Capability phải cung cấp | Output bắt buộc |
| --- | --- | --- |
| Capture | Camera stream, FFmpeg/decode, source PTS và bounded latest-frame queue | `camera_id`, `stream_epoch`, `frame_seq`, `source_pts`, frame contract |
| Detection | Detector hiện hành theo camera config, label/score/box và detect timestamp | Detection list gắn đúng frame lineage |
| Association | Norfair/centroid-compatible association, track ID, matched detection và track state | Ordered `TrackedObjectUpdate` |
| Lifecycle | Tạo, update, mất, kết thúc track; end-of-stream và epoch change | `track_start`, `track_update`, `track_end` typed events |
| Quality/evidence reference | Frame reference và bbox hợp lệ cho downstream recognition | Evidence ID/reference có TTL, không gửi path tùy ý |
| Transport | Bounded result queue, backpressure, stale-drop và reconnect state | Result stream có sequence/epoch và typed failure |

#### 5.6.1 Input/output contract

Input của tracker là camera source và cấu hình detector/tracking theo camera. Output không được
là raw track ID đơn lẻ. Mỗi update phải chứa tối thiểu:

```text
camera_id
stream_epoch
frame_seq
source_pts
track_id
label
score
object_bbox
frame_time
observed_in_frame
lineage/schema/config hash
```

`track_id` chỉ có ý nghĩa trong một cặp `(camera_id, stream_epoch)`; không được nối history qua
epoch mới. `source_pts` và `frame_seq` là căn cứ chọn evidence và đối chiếu passage. `track_end`
phải được phát khi object timeout, stream disconnect, shutdown hoặc epoch đổi; không chờ recognition
service trả kết quả.

#### 5.6.2 Ownership và giới hạn

Tracker sở hữu capture, detection, association, track lifecycle và result stream. Tracker không
được sở hữu:

- Face/LPR history, voting hoặc `RecognitionCore`;
- canonical Event, API, SQLite, media recording hoặc notification;
- recognition retry, winner selection hoặc publication;
- đọc/ghi evidence bằng filesystem path không có TTL và lineage.

Frigate nhận `TrackedObjectUpdate`, kiểm tra `(camera_id, stream_epoch, frame_seq, track_id)`,
sau đó cập nhật base Event path và chuyển candidate/evidence sang recognition boundary. Khi edge
chưa được triển khai, chính các lane này vẫn chạy trong Frigate main với cùng contract.

#### 5.6.3 Failure và backpressure contract

Tracker phải fail closed theo từng camera/epoch: queue đầy thì stale-drop theo policy đã cấu hình,
không phát lại frame cũ và không tạo track giả. Khi capture mất frame, tracker phát health/failure
và `track_end`; khi stream phục hồi, tạo `stream_epoch` mới và bắt đầu track mới. Reconnect không
được nối history cũ hoặc tái sử dụng sequence cũ.

Các hard gate trước khi tracker trở thành runtime riêng:

1. Không có `No frames received`, capture lag hoặc queue backlog vượt bound.
2. Detection/track result giữ đúng frame, PTS, bbox và camera/epoch lineage.
3. Không mất hoặc nhân đôi `track_start`, `track_update`, `track_end` trong replay.
4. Recognition candidate/evidence từ edge tương đương contract Frigate main hiện tại.
5. Event/API/SQLite/media vẫn chỉ được commit bởi Frigate; không có Event duplicate.
6. Stream disconnect/reconnect tạo typed failure, cleanup về `0`, rồi phục hồi serving.
7. CPU/GPU/queue/resource telemetry đủ để scale camera theo node edge.

## 6. Quality contract theo camera

Mỗi camera khai báo trực tiếp các ngưỡng mà `QualitySelector` sử dụng. Các giá trị dưới
đây chỉ là điểm bắt đầu và phải được chốt bằng ground-truth replay trên camera/hardware
thật.

```yaml
camera_quality:
  gate_lpr_01:
    task: lpr
    min_detail_width_px: 140
    max_yaw_deg: 25
    evidence: {width: 2560, height: 1440, fps: 20}

  lobby_face_01:
    task: face
    min_detail_width_px: 96
    max_yaw_deg: 35
    evidence: {width: 1920, height: 1080, fps: 15}
```

Selector ghi lý do reject để biết cần chỉnh camera, threshold hay model; không tự đổi
stream/model khi quality thấp.

## 7. Tách detect stream và evidence stream

Mỗi camera có tối đa ba vai trò độc lập:

| Stream | Mục đích | Đặc tính |
| --- | --- | --- |
| Detect | Motion/object/tracker | Resolution thấp, latest-frame, cho phép drop stale |
| Evidence | Best shot, LPR, face | Full-resolution, FPS theo profile, ring buffer bounded |
| Record | Playback/forensics | Bitrate và retention tối ưu cho lưu trữ |

“Tối đa ba vai trò” không có nghĩa phải mở ba RTSP session/decoder. V1 ưu tiên hai
input: detect stream thấp và một high-resolution stream dùng chung cho evidence/record
nếu codec, FPS, GOP và retention path đáp ứng cả hai contract. Chỉ tách evidence khỏi
record thành decoder riêng khi benchmark chỉ ra seek latency, GOP hoặc recording load
làm vi phạm evidence SLA.

`EvidenceRingBuffer` giữ frame trong một cửa sổ ngắn, ví dụ 3–10 giây. Buffer phải:

- Bounded theo byte và thời gian.
- Cho phép lấy high-resolution frame gần timestamp của track.
- Copy crop/frame đã được chọn ra khỏi ring buffer trước khi frame hết hạn.
- Ghi đè frame cũ; không tăng bộ nhớ theo thời gian.

Không ghi toàn bộ ring buffer vào database hoặc disk.

## 8. Frame reference tối thiểu

Candidate chỉ cần đủ thông tin để recognition dùng đúng ảnh đã được chấm quality:

```text
camera_id + track_id + frame_timestamp + frame_ref + object/detail bbox
```

Không tạo time model hoặc clock-drift subsystem riêng. Với local pipeline, `frame_ref` trỏ tới
frame/crop trong bounded buffer. Với external runtime, client copy đúng I420 frame thành bounded
gRPC evidence kèm shape/layout/byte length, evidence ID và expiry; service không dereference
filesystem path hoặc URL do caller cung cấp.

## 9. Unified Quality Selector

Face và LPR sử dụng chung contract candidate:

```python
EvidenceCandidate(
    candidate_id,
    camera_id,
    track_id,
    frame_time,
    frame_ref,
    object_box,
    detail_box,
    quality_score,
    quality_reasons,
)
```

Quality score gồm các thành phần có thể giải thích:

| Metric | LPR | Face |
| --- | --- | --- |
| Pixel density | Plate width/height | Face width/height |
| Blur | Laplacian/motion blur | Laplacian/motion blur |
| Exposure | Plate clipping | Face clipping/shadow |
| Pose | Plate perspective | Yaw/pitch/roll |
| Occlusion | Plate coverage | Eyes/nose/mouth visibility |
| Detector score | Plate/object | Face/person |
| Temporal stability | Track continuity | Track continuity |

Quality metric có thể ghi lý do như `plate_width_below_minimum`, blur hoặc pose để chẩn đoán và
chọn evidence hiển thị. Nó không được gate observation hay thay admission/voting của recognition
master.

## 10. Recognition decision policy

Policy active sau Phase 6-0 là trả Face/LPR recognition về đúng Frigate `master` tại Phase 6-1:

- LPR giữ rolling variant window, Jaro-Winkler clustering, cluster support và representative của
  master.
- Face giữ `person_face_history`, weighted-average voting, `min_faces`, count-tie rejection và
  attempt limits của master.
- Camera-specific trace/evidence/report được phép quan sát nhưng không được tham gia admission,
  history, winner, threshold hoặc publish timing.

Toàn bộ rolling top-3, best-result rank, top1-top2 margin, custom `SEARCHING/ACCEPTED/EXHAUSTED`
và passage-level consensus của Phase 5 đã bị xóa khỏi kiến trúc. Chúng không còn là phương án
deferred để tái sử dụng.

## 11. Capacity và overload control

### 11.1 Baseline compute hiện tại

Snapshot runtime ngày 2026-08-08 được đo trên HP Victus, Ryzen 5 5600H, RTX 3050
Laptop 4 GB, với hai camera 1280 × 720 và detect 5 FPS/camera. Cấu hình dùng YOLO
320 × 320 trên GPU, face model `large`, native plate detector và OCR.

| Stage | Thời gian trung bình/lần | Tần suất tại snapshot | Kết luận |
| --- | ---: | ---: | --- |
| Object detection người/xe | 9,20–9,75 ms | Chạy nền trên cả hai camera | Rẻ nhất mỗi lần nhưng tăng tuyến tính theo số camera/FPS |
| Plate detection | 23,06–25,72 ms | 4,3–5,5 lần/s | Chỉ là bước định vị trước OCR |
| Face recognition | 48,58–55,40 ms | 0,5 lần/s | Đắt mỗi candidate nhưng tải tổng thấp khi ít người |
| Plate OCR | 54,25–63,76 ms | 1,9–2,6 lần/s | Đắt nhất mỗi lần inference |

Trong các snapshot cùng phiên, `car_camera` dùng khoảng 42% CPU process, `face_camera`
khoảng 8%, embeddings/enrichment process khoảng 60–68%, GPU khoảng 33–34%, VRAM khoảng
44,6% và CPU toàn hệ thống Frigate khoảng 17,8–19,3%. Các tỷ lệ process không được cộng
trực tiếp thành tổng CPU hệ thống.

SQLite runtime có 139 passage xe kết thúc với duration trung vị 3,4 giây và 61 passage
người với duration trung vị 3,6 giây. Với nhịp snapshot cao nhất, một passage xe có thể
kích hoạt xấp xỉ 8–9 lần OCR, còn một passage người xấp xỉ 1,8 lần face recognition.
Đây là cơ sở cho projection, không phải phép đo attempts gắn chính xác theo từng passage.

Kết luận kiến trúc:

1. Native LPR là pipeline enrichment nặng nhất vì một passage phải qua object detect,
   tracking, plate detect, quality processing và tối đa ba OCR độc lập.
2. Face recognition đứng sau LPR về tải hiện tại. Face model vẫn đắt trên mỗi candidate,
   nhưng quality gate làm nó chạy ít hơn.
3. Object detection là chi phí nền quyết định số camera tối đa: inference đơn lẻ nhẹ
   nhất nhưng phải chạy liên tục trên mọi luồng.

### 11.2 Compute control không đổi recognition

Compute control chỉ được giới hạn transport, queue và concurrency ở nơi không làm mất hoặc đổi thứ
tự observation mà Frigate `master` sử dụng. Không còn cascade top-3, custom dedupe, calibrated
early-stop, best-result winner hoặc terminal recognition state trong kiến trúc.

Metric bắt buộc gồm calls/s, P95 latency, queue age/depth, CPU, GPU, VRAM và compute-time theo raw
track. Khi quá tải, runtime phải báo degraded state; không được âm thầm thay cadence/voting để đạt
capacity.

## 12. Passage-level SLA và observability

Đơn vị đo chính là vehicle/person passage, không phải frame.

```text
Physical passage
├── Object detected
├── Track continuous
├── Quality candidate accepted
├── Recognition committed
└── Event committed
```

Các KPI bắt buộc:

| KPI | Ý nghĩa |
| --- | --- |
| Passage detection recall | Tỷ lệ passage thật tạo event |
| Track continuity | Tỷ lệ passage không đổi nhầm ID |
| Quality acceptance | Tỷ lệ passage có evidence đạt profile |
| Recognition precision/recall | Đúng/sai theo ground truth |
| Evidence correctness | Result, bbox và frame cùng evidence |
| End-to-end latency | Capture đến recognition/Event committed |
| Degraded duration | Thời gian vi phạm camera/capacity contract |

FPS, detector inference và queue depth vẫn được thu thập nhưng chỉ là diagnostic metric.

### 12.1 Contract đo KPI recognition

Acceptance gate và báo cáo độ chính xác là hai output độc lập. Gate có thể fail vì thời gian,
RAM, cleanup hoặc evidence, nhưng các lỗi vận hành đó không được tự động biến thành lỗi OCR.
Ngược lại, một run khởi động ổn định không làm KPI recognition hợp lệ nếu scorer không chứng minh
được quan hệ giữa ground truth và candidate thực tế.

Mỗi trace runtime giữ cùng một lineage từ frame nguồn đến kết quả cuối:

```text
source PTS -> object bbox -> runtime track/generation -> candidate ID
           -> plate/face bbox -> raw outcome -> final publication
```

PTS và bbox là bằng chứng nội bộ để kiểm tra candidate/result/evidence có cùng frame, không phải
khóa gán ground truth LPR. Pipeline tự sinh trace và final publication trước; scorer LPR chỉ đối
chiếu plate đã publish của trace hoàn tất với danh sách `expected_plate` duy nhất. Fixture không
được dùng time/bbox để đổi ownership, tách hoặc hợp nhất trace. Face vẫn dùng passage nguồn vì
identity `unknown` không thể đối chiếu chỉ bằng chuỗi kết quả.

Ba replay round được chấm riêng trước khi aggregate. Mỗi passage/round chỉ có một final decision;
không dùng mode của mọi `event_published` làm representative. Báo cáo tối thiểu phải tách:

| Nhóm | KPI |
| --- | --- |
| Detection | car detection recall, track coverage, track-switch rate |
| Plate/face localization | detail detection recall trên đúng object |
| Recognition | attempted rate, conditional exact accuracy, unknown/ambiguous rate |
| End-to-end | exact precision, exact recall, wrong/missing publication rate |
| Stability | đúng bao nhiêu trên ba round cho từng physical passage |
| Debug ceiling | có ít nhất một raw outcome đúng; không gọi là production accuracy |

Nếu round/PTS alignment hoặc result lineage không hợp lệ, artifact phải ghi
`measurement_valid=false` và KPI bị ảnh hưởng là `null`. Runtime health/gate vẫn được báo ở phần
riêng, nhưng không được trộn vào tử số hoặc mẫu số accuracy.

### 12.2 Contract Platform runtime report hiện hành

Platform runtime test dùng report **evidence-only** xuyên suốt roadmap; các giá trị KPI/runtime
chỉ là diagnostic, không phải acceptance decision. Mỗi invocation chạy một vòng, lưu vào thư mục
timestamp riêng và chốt measurement sau EOF của mọi finite source. Contract chi tiết về raw trace,
source PTS, hardware metrics và artifact được mô tả tại
[tools/tests/README.md](../../tools/tests/README.md) và được thực thi bởi validator canonical.

## 13. Lộ trình triển khai

Quy ước trạng thái: `[DOING]` là phase đang triển khai; `[DONE]` nghĩa là toàn bộ phạm vi
implementation đã chốt của phase đã hoàn thành và có bằng chứng kiểm chứng tương ứng. Kết quả đo
của một phase được ghi nguyên trạng, kể cả khi chưa đạt mục tiêu cuối. `DONE` là `DONE`; kết quả
định lượng không đổi một phase đã hoàn thành về trạng thái khác.

`Acceptance tổng thể` là kết luận xuyên suốt toàn bộ roadmap, không phải trạng thái riêng của từng
phase. Checkbox chỉ được tick khi implementation/test/benchmark tương ứng thực sự đã chạy; không
tick chỉ vì code đã tồn tại.

Đây là lộ trình **triển khai kiến trúc**. Mỗi phase bên dưới phải tạo ra runtime contract,
module hoặc deployment topology được nêu rõ; unit test, replay và benchmark chỉ là bằng chứng
để đóng phase, không phải work package thay thế cho phần triển khai. Phase 1–4 đã hoàn tất;
Phase 5 là thử nghiệm `[SUPERSEDED]`; Phase 6 đã `[DONE]` với recognition core đồng bộ và Frigate
adapters; Phase 7 đã `[DONE]`: external container/runtime đã chạy cùng shared decision
code và E2E đạt `measurement_valid=true`, correlation `0` cùng cleanup/restore; fault injection và
wheel packaging Phase 7 đã có artifact. Phase 8 cũng đã
`[DONE]` trong phạm vi local edge tracker: tracker → Frigate main → recognition → Event/API/SQLite/
media đã qua healthy E2E chính thức, đủ 15 producer trace/clip và restore thành công. Acceptance
tổng thể được đánh giá riêng sau khi hoàn thành toàn bộ roadmap,
không dùng để đổi trạng thái từng
phase đã hoàn tất.

### 13.1 Ma trận truy vết thiết kế → triển khai

| Mục thiết kế | Requirement triển khai | Phase owner | Kết quả phải tồn tại |
| --- | --- | --- | --- |
| 1. Mục tiêu | On-premise Camera AI với recognition runtime độc lập và Event output ổn định | Phase 4–7 | Recognition container không sở hữu tracker/Event/media; adapter không đổi decision contract |
| 2. Baseline | Giữ Frigate capture/detect/track/Face/LPR/Event làm nền tảng | Phase 1–2 | Baseline và passage remediation có artifact `[DONE]` |
| 3. Khoảng trống | Passage, quality, evidence, recognition core, transport và trace có owner riêng | Phase 2–7 | Tracker → Frigate main → recognition có boundary rõ; không thêm parallel decision path |
| 4. Nguyên tắc | Lineage, explicit lifecycle, bounded admission, no fallback và single-writer ownership | Phase 3–7 | Overload/restart trả typed failure; không silent drop, retry chéo runtime hoặc duplicate publish |
| 5. Kiến trúc đích | `tracker → TrackedObjectUpdate/MediaManifest → frigate → RecognitionJob → camera-recognition → RecognitionOutcome → frigate` | Phase 6–9 | Tracker không gọi recognition trực tiếp; Frigate giữ Event/publication và proxy media edge |
| 6. Quality contract | Camera profile và reject reasons có schema/runtime owner | Phase 4 | Config validate được và selector xuất quality/reject reason |
| 7. Detect/evidence stream | Live detect dùng latest-frame; finite MP4 dùng FIFO/backpressure; evidence/record bounded riêng | Phase 4, 6-0 | Source role, frame ownership, source timeline và byte/time bound được triển khai |
| 8. Frame reference | Observation/result/evidence giữ cùng camera, track, epoch, frame và bbox | Phase 3–7 | External V1 dùng bounded raw I420 evidence, length/shape/layout validation và TTL |
| 9. Quality/evidence observability | Face/LPR có cùng lineage và diagnostic metric | Phase 4, 6-1 | Selector không gate observation hoặc tham gia recognition decision |
| 10. Recognition/result selection | Khôi phục nguyên vẹn LPR clustering và Face weighted voting từ Frigate master | Phase 6-1 | Khóa master commit, restore decision semantics và chứng minh parity |
| 11. Compute control | Core đồng bộ khóa parity trước; service dùng bounded ordered executor | Phase 6–7 | Queue full reject ngay; service không thay cadence, order hoặc recognition output của core |
| 12. Edge tracker runtime | Tách capture/detection/tracking thành edge service, chỉ phát `TrackedObjectUpdate` về Frigate main | Phase 8 | Local replay lineage, ordered lifecycle, cleanup/restore, edge media và single-owner Event contract được kiểm chứng |
| 13. Remote distributed runtime | Frigate main kết nối cả tracker và recognition chạy trên máy khác qua private network và mTLS | Phase 9 | Remote topology, node/camera ownership, certificate, epoch, publication safety và fault recovery được kiểm chứng |

Dependency bắt buộc:

```text
baseline/passage [DONE]
→ LPR execution foundation [DONE]
→ camera quality + evidence observability [DONE]
→ finite-source timeline + raw trace + native media [DONE: Phase 6-0]
→ Frigate master voting/consensus parity [DONE: Phase 6-1]
→ standalone synchronous recognition core + Frigate adapters [DONE: Phase 6]
→ external gRPC recognition container + Frigate host adapter [DONE: HEALTHY E2E]
→ restart/disconnect fault injection + wheel packaging [DONE: Phase 7]
→ edge tracker runtime + Frigate tracked-object adapter [DONE: Phase 8 LOCAL E2E]
→ remote tracker + remote recognition distributed deployment [PLANNED: Phase 9]
```

### Phase 8 — Edge tracker runtime [DONE — HEALTHY LOCAL E2E]

Phase 8 tách lane xử lý liên tục theo camera khỏi Frigate main thành `tracker` edge runtime. Tracker
nhận camera stream, decode và chạy lại chính các component Frigate hiện hành cho object detection,
Norfair association, `CameraState`/`TrackedObject`, zone/speed/path, PTZ và recording; không có
implementation thuật toán thứ hai. Với camera đã gán edge, tracker là media authority và phát
ordered typed update, evidence reference cùng media manifest về Frigate main. Frigate main
validate/idempotent ingest, giữ Event/API/SQLite/notification/publication SOT, route recognition và
proxy media; main không tính lại tracker decision. Tracker không giao tiếp trực tiếp với recognition.

Trạng thái chốt ngày 2026-08-14: contract/config ownership, gRPC/mTLS, shared producer,
`CameraState` edge adapter, bounded I420 evidence, durable SQLite journal, canonical ingest record,
edge media, managed node runtime và launcher readiness đã chạy trong topology Docker thật. Healthy
entrypoint `tools/tests/e2e/run_platform_runtime_test.py` tạo run `20260814-221312-066` với
`accepted=true`, `acceptance.status=passed`, `measurement_valid=true`, 4 Face trace, 11 LPR trace,
15 `clip.mp4`, 15 `trace.json`, correlation/API-SQLite mismatch `0`, pending/restart/bad runtime log
`0` và `runtime_restored=true`. Tổng runtime là `104.265` giây nội bộ, `106.026` giây theo host.
Development E2E dùng source bind mount và `--no-build`; tài liệu này không suy diễn một production
image build mới từ bằng chứng đó.

Phạm vi triển khai:

- **Phase 8-1 — Typed tracker contract:** định nghĩa `TrackedObjectUpdate` gồm `camera_id`,
  `stream_epoch`, `frame_seq`, `source_pts`, `track_id`, operation `track_start/update/end`, label,
  score, bbox, `frame_time`, `observed_in_frame`, schema/config hash và typed failure.
- **Phase 8-2 — Edge processing runtime:** đóng gói capture/FFmpeg, detector hiện hành và
  Norfair-compatible association/lifecycle thành container `tracker`; không đổi model, threshold,
  resolution hoặc tracking algorithm để ép acceptance.
- **Phase 8-3 — Frigate host adapter:** nhận và validate node/camera ownership, epoch, sequence,
  frame lineage và idempotency trước khi cập nhật base Event path; late, duplicate và old-epoch
  update bị loại trước Event/recognition publication.
- **Phase 8-4 — Evidence boundary:** chuyển frame/evidence reference bounded có TTL và đúng
  camera/epoch/frame/bbox lineage về Frigate; Frigate tiếp tục tạo recognition job và giữ evidence
  đến terminal acknowledgement/expiry.
- **Phase 8-5 — Runtime control:** health/readiness, bounded queue, backpressure, stale-drop,
  reconnect, explicit `track_end`, config/schema compatibility và CPU/GPU/queue telemetry theo node.
- **Phase 8-6 — Deployment:** thêm Docker image/service và launcher contract cho một hoặc nhiều
  edge node; mỗi camera tại một thời điểm chỉ có đúng một tracker owner và restore được topology
  Frigate-contained hiện hành.
- **Phase 8-7 — Acceptance:** chạy physical-source replay và healthy local E2E; artifact ghi topology,
  node/camera ownership, source/worktree hash, epoch, lifecycle count, queue/resource state, media,
  cleanup và restore. Fault campaign cho network/remote deployment thuộc Phase 9; không được ghi là
  đã chạy trong lần đóng Phase 8 này.

Tiêu chí đóng Phase 8:

- Detection/tracking dùng lại component Frigate hiện hành, không đổi model/threshold/resolution;
  local replay giữ đủ 4 Face trace và 11 car trace. Face/LPR exact accuracy tiếp tục là diagnostic.
- Không mất hoặc nhân đôi `track_start`, `track_update`, `track_end`; không nối track/history qua
  `stream_epoch` mới và không nhận late/old-epoch result.
- Tracker không gọi trực tiếp `camera-recognition`, không sở hữu Event/API/canonical SQLite/
  notification/publication và không tạo publication path thứ hai. Tracker là media authority cho
  camera edge; Frigate giữ manifest/reference. Local acceptance materialize report media qua bind
  mount; authenticated remote retrieval được kiểm chứng trong Phase 9.
- Source contract giữ typed failure, epoch và bounded reconnect cho tracker unavailable; fault E2E
  đa máy được kiểm chứng ở Phase 9 thay vì là gate đóng local runtime.
- Queue/in-flight/evidence/writer/session terminal về `0`, không backlog unbounded, không duplicate
  Event và runtime restore thành công.
- Targeted launcher/contract, Ruff/diff-check và healthy E2E chạy qua launcher chuẩn; mọi artifact
  truy vết được config/source hash. Production image build vẫn là release gate riêng.

### Phase 9 — Remote distributed runtime [PLANNED]

Phase 9 triển khai cả `tracker` và `camera-recognition` trên máy khác với Frigate main. Phase này
chỉ bắt đầu sau khi Phase 8 khóa `TrackedObjectUpdate`, evidence và lifecycle contract. Frigate main
vẫn là system of record và integration hub: remote tracker chỉ gửi tracked-object stream về Frigate;
Frigate mới gửi recognition job tới remote recognition. Hai remote service không gọi trực tiếp nhau.

Phạm vi đã khóa:

- Thêm remote endpoint DNS/IP và deployment mode độc lập cho từng `tracker` node và
  `camera-recognition`; launcher Frigate không start/inspect container remote như container local.
- Giữ transport production qua gRPC/mTLS trên private LAN/VPN; preflight kiểm tra DNS, TCP,
  certificate SAN, CA/client certificate, server name, expiry, capabilities và schema/config hash
  cho cả hai service.
- Cấu hình ánh xạ mỗi camera tới đúng một tracker node; artifact ghi endpoint đã redacted,
  node/camera ownership, tracker `stream_epoch`, recognition `service_epoch`, topology hash,
  readiness và restore result.
- Remote healthy E2E phải đi xuyên `remote tracker → Frigate → remote recognition → Frigate`, giữ
  raw passage lineage, canonical Event publication, API/SQLite consistency, bounded queue và cleanup
  zero.
- Fault acceptance chạy độc lập cho tracker restart, recognition restart, network partition trên
  từng link và Frigate client disconnect; late/duplicate/old-epoch update hoặc outcome phải bị loại
  trước publication.
- Frigate main phải tiếp tục phục vụ Event/API/media khi một remote service unavailable; tracker
  failure không được gọi recognition trực tiếp hoặc nối track history cũ, recognition failure không
  được kích hoạt local fallback.
- Bổ sung observability cho health transition, end-to-end latency, queue/in-flight, stale result,
  per-node resource và certificate expiry. HA/failover và tự động chuyển camera sang node khác là
  scope riêng, chưa thuộc Phase 9.

Tiêu chí đóng Phase 9:

- Unit/contract, compile/Ruff/diff-check và launcher/preflight tests pass trước build/deploy.
- Remote healthy E2E và toàn bộ fault E2E chạy qua entrypoint/launcher chuẩn, có artifact mới cùng
  source commit/worktree hash và topology hash.
- Không direct tracker-to-recognition path, local fallback, duplicate Event, stale/old-epoch
  publication, reconnect loop hoặc nối history qua node/epoch mới.
- Runtime restore thành công; tracker/recognition queue, pending, in-flight, evidence, session và
  writer terminal đều về `0`; TLS/network failure trả typed failure đúng service boundary.

### Phase 1 — Đo baseline hai camera [DONE]

- [x] Xây passage dataset/replay cho LPR và face.
- [x] Đo calls/s, latency, queue, GPU/CPU/VRAM và recognition theo passage trên pipeline
  hiện tại.
- [x] Xác nhận result/bbox/crop thuộc cùng candidate trước khi dùng baseline để so sánh.

Tiêu chí kiểm chứng Phase 1:

- [x] Có baseline lặp lại được cho một LPR camera và một face camera.
- [x] Báo cáo được passage recall, recognition precision/recall và end-to-end latency.

Bằng chứng: [summary.json](../../.tmp/platform-phase1/summary.json). Mỗi replay case
hoàn tất trong 58,45–60,02 giây. Face known/unknown đạt precision và recall 100%;
LPR passage detection recall baseline là 60%. Exact-match LPR là `null` vì clip 720p
không có passage nào đủ rõ để gán ký tự bằng mắt (`readable_denominator=0`); chỉ số này
được báo cáo nhưng không phải gate của Phase 1. Capture-to-recognition P95 theo physical
passage của face là 8,20 giây, trong khi gate sau khi candidate đủ điều kiện đạt first
attempt 309,1 ms và confirmed 629,5 ms.

Time budget `<120 giây` áp dụng cho unit, integration và replay kiểm chứng dùng trong vòng
lặp phát triển.

KPI baseline cần theo dõi khi triển khai Phase 2:

| Mức ưu tiên | KPI | Baseline Phase 1 | Hướng cải thiện | Vai trò |
| --- | --- | --- | --- | --- |
| 1 | LPR passage detection recall | 60% | Tăng recall và giữ passage precision ở ngưỡng product profile | KPI chính; đang bỏ sót 40% passage ground truth |
| 1 | Face capture-to-recognition P95 theo physical passage | 8,20 giây | Giảm rõ rệt thời gian chờ candidate đủ điều kiện | KPI end-to-end chính |
| 1 | Face recognition precision/recall | 100% / 100% trên known và unknown fixture | Theo dõi regression theo ngưỡng passage phù hợp với kích thước tập thử | Quality guardrail |
| 1 | Result/candidate correlation | Face correlation đạt; LPR plate box hợp lệ | Không có identity/plate gắn nhầm bbox, crop hoặc frame | Correctness gate |
| 2 | Face first attempt / confirmed | 309,1 ms / 629,5 ms | Giữ dưới gate 750 ms / 1.500 ms và giảm nếu không mất recall | Latency sau khi candidate đủ điều kiện |
| 2 | Pending queue cuối test | 0 | Luôn trở về 0 sau burst, không tích lũy stale candidate | Capacity/stability gate |
| 2 | Enrichment calls/s | Face 1,2; OCR 2,4; plate detection 5,6 | Giảm lần gọi thừa nhưng không giảm passage recall/precision | Compute-efficiency KPI |
| 2 | GPU / VRAM / RAM | 58%; 1.272/4.096 MiB; khoảng 4/7 GiB | Tạo headroom để tăng camera, không OOM hoặc tăng dần theo thời gian | Capacity KPI |
| 3 | Camera/process FPS | Khoảng 5 FPS; process FPS tối thiểu 4,7 | Giữ camera 4,5–5,5 FPS, process ≥4,5 FPS, skipped ≤0,5 FPS | Diagnostic guardrail, không thay cho passage KPI |
| 3 | Detector inference | Tối đa 16,61 ms | Giữ dưới 200 ms | Diagnostic guardrail |
| Chưa đo được | LPR exact-match | `null`, readable denominator = 0 | Bổ sung fixture 720p có biển đọc được trước khi dùng làm quality gate | Chỉ số bắt buộc báo cáo, chưa phải gate Phase 1 |

### Phase 2 — Sửa passage bottleneck [DONE]

Phase 2 đã hoàn tất theo [summary kiểm chứng](../../.tmp/platform-phase2/summary.json) với
`accepted=true` trong 113,406 giây. Đây là artifact lịch sử của Phase 2; cơ chế composite/loop cũ
không còn là runtime test hiện hành. Từ Phase 6-0, [validator entrypoint](../../tools/tests/e2e/run_platform_runtime_test.py)
chạy một vòng finite MP4 trực tiếp theo contract source-index/EOF tại mục 13. Runtime Phase 2 từng
đặt car `min_initialized: 1`, cho phép LPR ngay khi track hợp lệ, mở rộng crop có clamp, giữ
consensus representative hiện có và đưa cadence face về 200 ms.

Mục tiêu của Phase 2 là cải thiện trực tiếp hai bottleneck đã đo ở Phase 1: LPR passage
recall 60% và face capture-to-recognition P95 8,20 giây. Phase này không xây shared
quality selector, top-K hoặc evidence buffer mới.

- [x] Xác minh trực quan timestamp/replay phase của các passage LPR bị bỏ sót, sau đó bổ
  sung passage funnel có số đếm theo từng tầng:
  `ground truth -> object detected -> track created/continued -> candidate qualified -> recognition -> Event`.
- [x] Sửa tầng làm mất passage bằng detector/ROI/zone, cadence và tham số tracker hiện có;
  giữ nguyên detector/OCR model và resolution 720p trong Phase 2.
- [x] Đo và giảm thời gian `passage -> track -> candidate qualified` bằng cadence và logic
  chọn candidate hiện có; không lấy inference latency riêng lẻ làm kết quả end-to-end.
- [x] Bổ sung fixture 720p có passage đọc được bằng mắt và `expected_plate` đã chuẩn hóa để
  đo LPR exact-match; threshold confidence chỉ được chốt sau khi calibration bằng ground truth.

Tiêu chí kiểm chứng Phase 2:

- [x] Mọi replay test case của Phase 2 kết thúc trong dưới 120 giây.
- [x] LPR báo cáo đầy đủ số lượng và tỷ lệ chuyển đổi ở từng tầng passage funnel; passage
  recall cao hơn baseline 60% và passage precision đạt ngưỡng 80%. Một passage không được detector
  hoặc tracker tạo ra không được ghi nhận là lỗi OCR/consensus.
- [x] Fixture LPR readable có denominator lớn hơn 0 và báo cáo exact-match; unreadable
  passage vẫn tính vào passage recall nhưng không tính vào exact-match denominator.
- [x] Face capture-to-recognition P95 theo physical passage giảm so với baseline 8,20 giây;
  đồng thời detection recall, precision và recall đạt ngưỡng passage 80%.
- [x] Báo cáo riêng thời gian `passage -> track`, `track -> candidate qualified`, first
  attempt và confirmed; không dùng riêng inference latency để đại diện end-to-end latency.

Kết quả chốt Phase 2: LPR passage recall tăng từ 60% lên 100% trên 5 passage, không có
false passage; Face detection recall, precision và recall đều 100%; Face
passage-to-confirmed P95 là 1.676,8 ms, pending cuối test bằng 0, không restart/reconnect/
stall, RAM tối đa 4,29 GiB và SHM 14%. API và SQLite không có giá trị một phía hoặc lệch
nhau; một enrichment update không được cả hai store giữ lại được báo riêng, không bị gọi
nhầm là API–SQLite mismatch.

#### Kết quả Phase 2 theo mục tiêu ban đầu

Các chỉ số dưới đây là kết quả replay dùng để xác nhận hai bottleneck của
Phase 2 đã được khắc phục so với baseline.

| Gate/KPI | Kết quả hiện tại | Ngưỡng Phase 2 | Bằng chứng |
| --- | ---: | ---: | --- |
| Kết quả validator | `accepted=true`, 113,406 giây | `<119` giây, mọi hard gate đạt | [summary.json](../../.tmp/platform-phase2/summary.json) |
| LPR passage recall | 100% trên 5 passage; 3/3 vòng có track cho từng passage | Recall cao hơn baseline 60%; passage precision `≥80%` | [lpr.json](../../.tmp/platform-phase2/lpr.json), [runtime trace](../../.tmp/platform-phase2/runtime-trace.json) |
| Face detection/precision/recall | 100% / 100% / 100% | Mỗi chỉ số `≥80%` theo physical passage | [face.json](../../.tmp/platform-phase2/face.json) |
| Face passage-to-confirmed P95 | 1.676,8 ms | `<3.000` ms và thấp hơn baseline 8,20 giây | [face.json](../../.tmp/platform-phase2/face.json) |
| Face eligible-to-confirmed / first-attempt / embedding P95 | 621,6 / 147,3 / 44,9 ms | `≤1.500` / `≤750` / `≤200` ms | [face.json](../../.tmp/platform-phase2/face.json) |
| Runtime | pending 0; restart 0; RAM 4,29 GiB; SHM 14%; không reconnect/stall | pending 0; RAM `≤7 GiB`; SHM `<70%`; runtime ổn định | [summary.json](../../.tmp/platform-phase2/summary.json) |

#### Hạn chế hiện tại

| Hạn chế | Hiện trạng và ảnh hưởng | Hướng xử lý | Bằng chứng |
| --- | --- | --- | --- |
| Chất lượng LPR recognition chưa ổn định | Exact-match đạt 1/3 passage có biển đọc được; `lpr-01` và `lpr-02` chưa tạo kết quả OCR hợp lệ. Hệ thống đã bắt được passage nhưng chưa đọc tin cậy mọi biển trong tập thử. | Phase 3 sửa execution architecture và temporal decision hiện có. Nếu raw OCR không có thông tin hữu ích thì dừng ở ranh giới model, không tiếp tục vá pipeline. | [lpr.json](../../.tmp/platform-phase2/lpr.json), [evidence lpr-01](../../.tmp/platform-phase2/mismatches/lpr-01.jpg), [evidence lpr-02](../../.tmp/platform-phase2/mismatches/lpr-02.jpg) |
| Dữ liệu replay còn nhỏ | Kết quả hiện tại đủ xác nhận regression của Phase 2 nhưng chưa đủ để công bố accuracy tổng quát cho nhiều người, biển số và điều kiện hình ảnh. | Đây là đầu vào cho fine-tune/certification sau này, không phải work package triển khai của Phase 3. | [manifest](../../tools/fixtures/platform_passage_ground_truth.yaml) |

### Phase 3 — LPR execution foundation [DONE]

**Owner thiết kế:** mục 4, 8, 10 và phần LPR của mục 11. Phase này sửa execution path hiện
có trước khi thêm quality/evidence source mới. Không đổi model, resolution hoặc Face logic.

Luồng triển khai:

```text
object update
→ bounded latest-per-track worker
→ plate detector/OCR
→ PlateObservation
→ PlateTrackState
→ optional PlateCommit
→ worker persist evidence
→ maintainer publish Event
```

- [x] Tạo typed `LprTrackKey(camera, track_id, generation)`, `PlateObservation`,
  `PlateTrackState` và `PlateCommit`; history bounded và dedupe theo frame reference.
- [x] Tách detector/OCR, temporal state reducer và Event side effect khỏi `lpr_process()`.
- [x] Chuyển LPR sang latest-per-track worker; candidate cũ bị replace/drop theo capacity/TTL,
  expire và generation mới vô hiệu toàn bộ stale work.
- [x] Chỉ encode evidence và stage commit khi decision mới hoặc mạnh hơn; maintainer chỉ
  publish một revision idempotent sang Event/API/SQLite.
- [x] Đưa observation hợp lệ vào temporal history trước commit threshold; consensus dùng
  unique-frame support, character confidence và text area. Nếu raw OCR không có thông tin
  hữu ích thì dừng ở model boundary, không thêm heuristic.
- [x] Xuất pending, replaced, capacity/TTL drops, stale-generation drops, candidate age và
  worker latency trong stats runtime.

Hoàn thành khi LPR inference/I/O không block maintainer, queue luôn bounded, stale generation
không publish được, một decision chỉ tạo một commit và regression replay giữ passage/Face KPI
trong ngưỡng Phase 2.

#### Kết quả Phase 3 so với cuối Phase 2

Run kiểm chứng cuối chạy trên image `camera-frigate:overlay-0c53e795ecdc`, hoàn tất trong
110,492 giây với `accepted=true`. Manifest passage và model hash giữ nguyên so với Phase 2;
generated config hash khác nên đây là regression comparison cùng fixture/model, không phải A/B
bit-identical tuyệt đối.

| Gate/KPI | Cuối Phase 2 | Cuối Phase 3 | Kết luận |
| --- | ---: | ---: | --- |
| Kết quả validator | `accepted=true`, 113,406 giây | `accepted=true`, 110,492 giây | Giữ toàn bộ hard gate, nhanh hơn 2,914 giây |
| LPR passage recall | 100% | 100% | Không regression passage |
| LPR passage precision | Chưa có field trong schema cũ | 100% | Không có false passage |
| LPR exact-match | 33,3% trên denominator 3 | 33,3% trên denominator 3 | Execution refactor không cải thiện OCR/model boundary |
| LPR consistency tối thiểu | 33,3% | 33,3% | Aggregate guardrail giữ nguyên |
| Face detection/precision/recall | 100% / 100% / 100% | 100% / 100% / 100% | Không regression Face runtime |
| Face passage-to-confirmed P95 | 1.676,8 ms | 1.595,6 ms | Cải thiện 81,2 ms |
| Pending / restart / bad log | 0 / 0 / 0 | 0 / 0 / 0 | Không backlog, restart hoặc stall |
| API–SQLite / correlation mismatch | 0 / 0 | 0 / 0 | Giữ external Event contract |
| RAM tối đa / SHM | 4.602.057.457 byte / 14% | 4.598.836.232 byte / 14% | Không tăng tài nguyên đáng kể |

Chi tiết LPR cho thấy quality residual vẫn thuộc model/input boundary: `lpr-01` và `lpr-02`
tiếp tục có passage/track nhưng không tạo OCR hữu ích; `lpr-06` vẫn exact-match `BEE3975`
nhưng consistency giảm từ 87,5% xuống 75%; `lpr-07` giữ consistency 100% nhưng representative
đổi từ `FKH9211` thành `FKH921` và recognized rounds giảm từ 2 xuống 1. Vì aggregate passage,
exact-match và stability gate không giảm, Phase 3 dừng đúng ở execution architecture, không vá
threshold hoặc thêm QualitySelector/evidence buffer thuộc Phase 4.

Bằng chứng triển khai: 24/24 test LPR/deferred/maintainer/stats đạt; fake inference chậm xác
nhận `process_frame()` không chờ inference; queue/state/control đều bounded; expire chặn stale
in-flight result. Run kiểm chứng cuối có pending 0, restart 0, không reconnect/stall và runtime sau
restore healthy. Hai unit assertion Face riêng còn lỗi ở fixture `transaction_id` và kỳ vọng
frame 100.5; các đường code đó không nằm trong diff Phase 3 và Face runtime vẫn đạt gate.
Summary kiểm chứng hiện chưa lưu camera `skipped` thành một field so sánh Phase 2/3; gate này
chỉ có bằng chứng gián tiếp từ pending 0, không stall và passage recall giữ 100%, không được coi
là phép đo trực tiếp `skipped`.

Bằng chứng: [Phase 2 summary](../../.tmp/platform-phase2/summary.json),
[Phase 3 summary](../../.tmp/platform-passage/summary.json),
[Phase 3 LPR result](../../.tmp/platform-passage/lpr.json),
[Phase 3 runtime trace](../../.tmp/platform-passage/runtime-trace.json).

### Phase 4 — Camera quality, evidence và shared candidate contract [DONE]

**Owner thiết kế:** mục 3–9. Đây là phase triển khai các thành phần đang có trong kiến trúc
đích nhưng trước đây không có owner.

- [x] Khai báo quality config theo camera, `FrameRef`, `EvidenceLease` và
  `EvidenceCandidate` dùng chung cho Face/LPR;
  schema gồm camera/track/generation/frame, object/detail bbox, source role, quality và reject
  reasons.
- [x] Map rõ `detect`, `evidence` và `record` role. Phase 4 chỉ cho phép `detect`; config
  `evidence`/`record` fail rõ vì chưa có high-resolution adapter và không mở decoder thứ ba.
- [x] Tạo `EvidenceRingBuffer` bounded theo byte và thời gian; frame hết hạn bị ghi đè, candidate
  được chọn phải sở hữu crop/reference trước expiry.
- [x] Triển khai `QualitySelector` nhận profile theo camera và chấm pixel density, blur, exposure,
  pose/occlusion khi metric có sẵn. Đây là artifact Phase 4; sau Phase 6-1 nó chỉ được dùng cho
  evidence/diagnostic, không được lọc observation của recognition.
- [x] Bổ sung `EvidenceCandidate` để kiểm chứng lineage frame/bbox. Contract này không được thay
  admission, cadence, voting hoặc publication của master.
- [x] Reject reason và quality metrics đi vào passage observability; selector không tự đổi
  model/resolution khi input không đạt profile.

Hoàn thành khi cùng một candidate contract chạy được cho Face và LPR trong runtime hiện tại,
memory bounded theo cấu hình, result/evidence giữ đúng lineage và không còn quality path riêng biệt.

#### Kết quả triển khai và kiểm chứng Phase 4

Image Phase 4 `camera-frigate:overlay-7c31cfa85448` đã build thành công. Bộ test tập trung trong
image đạt 40/40 cho evidence, quality, LPR deferred/state/association, Face candidate và snapshot;
19/19 test validator trên host đạt. Compile, Ruff cho module mới và `git diff --check` đều đạt.

Run kiểm chứng Phase 4 sau khi sửa gate FPS hoàn tất trong 109,666 giây với `accepted=true`. Gate
`skipped_fps` dùng regression budget theo control thay cho ngưỡng tuyệt đối không có trong
baseline: mỗi camera không được tăng quá 0,1 FPS. Control Phase 3 đã ở mức 3,3/3,9; Phase 4 đạt
3,4/3,2 nên không regression. Giá trị tuyệt đối vẫn được lưu làm diagnostic và backlog capacity,
nhưng không bắt Phase 4 sửa vấn đề detect throughput tồn tại từ baseline.

| Gate/KPI | Control Phase 3 | Phase 4 | Kết luận |
| --- | ---: | ---: | --- |
| Kết quả validator | Control baseline | `accepted=true`, 109,666 giây | Đạt toàn bộ hard gate Phase 4 |
| LPR recall / precision / exact-match | 100% / 100% / 33,3% | 100% / 100% / 33,3% | Không regression recognition |
| Face detection / precision / recall | 100% / 100% / 100% | 80% / 100% / 80% | Đạt gate Phase 3 tối thiểu 80% |
| Pending / restart / reconnect-stall | 0 / 0 / 0 | 0 / 0 / 0 | Đạt |
| API-SQLite / correlation-lineage mismatch | Phase 3 chưa có candidate lineage | 0 / 0 | Shared candidate lineage đạt |
| `skipped_fps` face / car | 3,3 / 3,9 | 3,4 / 3,2 | Đạt regression budget tối đa +0,1 mỗi camera |
| Evidence tối đa face / car | 0 / 0 | 16.588.800 / 20.736.000 byte | Dưới 32 MiB mỗi camera |
| RAM tối đa | 4.603.131.199 byte | 4.655.744.548 byte | Tăng 52.613.349 byte, dưới budget tăng 128 MiB và tổng dưới 7 GiB |

Phase 4 vì vậy được đóng `[DONE]`. Việc giảm `skipped_fps` tuyệt đối được chuyển thành backlog
capacity riêng; validator vẫn fail nếu Phase 4 làm bất kỳ camera nào tăng quá 0,1 FPS so với
control. Quy trình kiểm chứng đã restore runtime config và image trước khi kết thúc.

Bằng chứng: [Phase 3 control summary](../../.tmp/platform-phase4-control/summary.json),
[Phase 4 summary](../../.tmp/platform-phase4/summary.json),
[Phase 4 Face result](../../.tmp/platform-phase4/face.json),
[Phase 4 LPR result](../../.tmp/platform-phase4/lpr.json),
[Phase 4 runtime trace](../../.tmp/platform-phase4/runtime-trace.json).

### Phase 5 — Custom recognition lifecycle và compute control [SUPERSEDED]

Phase 5 đã đóng như một vòng thử nghiệm ngày 09/08/2026, nhưng kiến trúc custom không còn là
production target. Phase 6-1 sẽ trả recognition voting/consensus về Frigate `master`. Các mô tả
cũ về passage registry, `PreparedPlateCandidate`, cửa sổ 0,4 giây, rolling top-K/max-three,
strict consensus, `BestResultReducer`, Face top1-top2 margin và custom terminal lifecycle đã bị
loại khỏi kiến trúc hiện hành.

Artifact Phase 5 chỉ còn là bằng chứng lịch sử, không phải source triển khai:
[aggregate Phase 5.1](../../.tmp/platform-phase5-1/aggregate-summary.json) và
[aggregate Phase 5.2](../../.tmp/platform-phase5-2/aggregate-summary.json).

#### Audit source Phase 5 → master ngày 11/08/2026

So với `upstream/master@50a2b6729eb152d9512b100c78c55fa84dffa430`:

| Khu vực | Custom code còn tồn tại | Production target |
| --- | --- | --- |
| LPR | Realtime variants/clustering đã gần master, nhưng cluster tie-break và custom lifecycle còn lệch | Restore exact `(cluster_size, max_confidence)`, highest-confidence representative và publish timing |
| Face | Detection-frame pipeline, `BestResultReducer`, margin và max-three đang thay weighted voting | Restore `person_face_history`, weighted average, active `min_faces`, count-tie rejection và limits 12/6 |
| Config | `min_faces` bị deprecated; custom quality/lifecycle keys còn tồn tại | Khôi phục master decision fields; custom key không điều khiển recognition |
| Shared modules | Recognition/Face/LPR pipeline, association, evidence và quality custom vẫn còn | Chỉ giữ side-channel trace/report; bypass hoặc xóa decision owner cạnh tranh |
| Event/media | Event/API/SQLite, raw tracker ID, native clips và report | Giữ nguyên contract Phase 6-0 |

Không checkout đè toàn file vì phải giữ Camera-specific trace/media và dirty change ngoài
recognition. Code custom còn tồn tại không được hiểu là kiến trúc đã phê duyệt.

#### Boundary hiện hành trước Phase 6-1

| Khối | Trạng thái | Kết luận |
| --- | --- | --- |
| Phase 1–4 foundation | `[DONE]` | Giữ contract không can thiệp recognition decision |
| Phase 5 custom recognition | `[SUPERSEDED]` | Không tiếp tục top-K/best-result/strict-consensus |
| Phase 6-0 runtime/trace/media | `[DONE]` | 11 raw LPR traces, 11/11 clips và report đã xác minh |
| Phase 6-1 master parity | `[DONE]` | Exact LPR clustering và Face weighted voting đã có differential test |
| Phase 6 recognition core/adapters | `[DONE]` | Production adapter/core duy nhất đã test; finite-source caller phát explicit end sau EOF và runtime cleanup đạt zero |
| Phase 7 external recognition runtime | `[DONE]` | Docker/gRPC/mTLS service, host integration, shared logic, healthy E2E, ba fault scenarios và reproducible wheel packaging đã có artifact; LPR accuracy giữ ở diagnostic |

### Phase 6 — Master-compatible standalone recognition core [DONE]

**Owner thiết kế:** mục 5, 10 và 12. Phase 6 không kế thừa custom decision contract Phase 5.
Phase này restore master parity, sau đó tách LPR/Face thành core đồng bộ có thể import/nhúng độc lập
với detection subscriber, Event, database và notification. External Docker/gRPC runtime thuộc Phase 7.

#### Phase 6-0 — Finite-source runtime, raw trace lineage và native media [DONE]

Phase 6-0 hiện dùng đường LPR/Face thật của Frigate và chỉ bổ sung khả năng đưa finite local MP4
vào đúng capture/detect/track pipeline. Test không dựng tracker thứ hai, không tạo passage registry,
không sinh `p2/p3`, không gán trace bằng fixture time/bbox và không truy ngược từ OCR cuối cùng để
gom frame hoặc cắt video. Phase kế tiếp trả recognition voting/consensus về Frigate master.

Contract đã triển khai:

1. **Nguồn và thứ tự frame.** Finite local MP4 được decode trực tiếp ở resolution/FPS của nguồn,
   đưa vào capture queue theo FIFO/backpressure và không thay frame cũ bằng frame mới khi consumer
   chậm. Live/network input vẫn dùng latest-only để giới hạn latency. Hai loại nguồn không được dùng
   chung một drop policy.
2. **Timeline.** `frame_time = source_epoch + frame_number / detect_fps`; timestamp không phụ thuộc
   tốc độ wall-clock của decoder. Vì vậy detect, tracker, Event, recording và report cùng nằm trên
   một source-index timeline, kể cả khi inference tạo backpressure.
3. **Ranh giới phép đo.** Producer ghi `{camera}.start` ở frame đầu và `{camera}.end` sau khi enqueue
   frame cuối. Validator chỉ đặt `capture_cutoff` sau khi nhận đủ EOF marker và `latest.jpg` của mỗi
   camera đã đi qua timestamp cuối; không dùng duration ước lượng để chốt sớm.
4. **Trace identity.** `trace_id` LPR là raw Frigate tracked-object ID do tracker tạo và giữ nguyên
   qua detector, LPR, Event, recording và report. Fixture `lpr-01…lpr-11` chỉ chứa expected plate để
   so sánh terminal output sau cùng; fixture không tạo, nối, tách hoặc đổi tên runtime trace.
5. **Media ownership.** Clip được Frigate recording/export tạo từ lifecycle của chính trace và được
   lưu tại `media/lpr/<sanitized-trace-id>/clip.mp4` hoặc `media/face/<trace-id>/clip.mp4`, cạnh
   `trace.json` và evidence của trace đó. Thư mục con bên trong trace là `evidence_id` của một lần
   xử lý/candidate, không phải track mới. Validator không tạo asset ở staging rồi di chuyển/xóa sau.
6. **Báo cáo.** Mỗi invocation chạy một vòng và tạo thư mục timestamp riêng, gồm `report.md`,
   `summary.json`, log, hardware/queue metrics và media theo trace. Report là evidence-only, không có
   acceptance threshold; stage thiếu ghi `MISSING`/`-` và không tạo ảnh giả thay thế.

Các file sở hữu contract hiện hành:

| Thành phần | Owner |
| --- | --- |
| Finite MP4 capture, FIFO và source-index timestamp | `frigate/src/frigate/domain/video/ffmpeg.py`, `frigate/tests/test_video.py` |
| EOF/process synchronization, report và trace media | `tools/runtime/validate_platform_runtime.py` |
| Raw tracker/Event/recording lifecycle | Frigate detect/track/Event pipeline hiện có; validator chỉ đọc kết quả |
| Plate-only audit fixture | `tools/fixtures/platform_passage_ground_truth.yaml`, `tools/tests/unit/test_passage_acceptance.py` |

**Bằng chứng hiện tại ngày 11/08/2026:** run
[`20260811-044205-810`](../../.tmp/platform-runtime/20260811-044205-810/report.md) hoàn tất trong
`129,302 s`, `runtime_restored=true`, không restart, `car_camera skipped_fps=0,0`, có đúng 11 raw
LPR trace folder và 11/11 native LPR clip. Tổng native media là 12/12 khi tính thêm một Face trace.
Ba regression cho capture queue/timeline và 44 unit test passage acceptance đã đạt.

**Tồn đọng không được che:** report plate-only ghi exact match `7/11`, Face chưa nhận diện đúng và
`evidence_pinned_zero=false`. Visual lineage audit còn thấy một LPR invocation tiếp tục dùng bbox dự
đoán đã stale sau khi xe gốc rời khung, khiến OCR của xe sau có thể xuất hiện trên trace trước. Đây
là lỗi downstream LPR admission/representative timing; không phải lý do đổi raw tracker ID, ghép
trace hậu kỳ hay sửa fixture. Phase 6-0 hoàn tất source/trace/native-media runtime; trạng thái
`[DONE]` không tuyên bố recognition đã hoàn tất.

#### Phase 6-1 — Trả recognition voting/consensus về Frigate master [DONE]

**Nguồn chuẩn:** `blakeblackshear/frigate` nhánh `master`, commit
`50a2b6729eb152d9512b100c78c55fa84dffa430`. Local `upstream/master` và remote master đã được
đối chiếu cùng commit ngày 11/08/2026. File chuẩn gồm
[LPR realtime](https://github.com/blakeblackshear/frigate/blob/master/frigate/data_processing/real_time/license_plate.py),
[LPR mixin](https://github.com/blakeblackshear/frigate/blob/master/frigate/data_processing/common/license_plate/mixin.py),
[Face realtime](https://github.com/blakeblackshear/frigate/blob/master/frigate/data_processing/real_time/face.py)
và [classification config](https://github.com/blakeblackshear/frigate/blob/master/frigate/config/classification.py).

Phase này hủy hướng đưa rolling top-3/`BestResultReducer` thành production decision. Mục tiêu là
khôi phục đúng semantics master, không viết một thuật toán voting “tương đương”. Trace, evidence và
report có thể được giữ như side-channel nhưng không được thay admission, attempt cadence, history,
winner, score hoặc thời điểm publish của master.

**LPR master contract:**

- `LicensePlateRealTimeProcessor.process_frame()` gọi trực tiếp `lpr_process()` trên canonical
  tracked-object update.
- Mỗi OCR vượt `recognition_threshold` được thêm vào variant history của Event; cửa sổ bounded là
  `detect.fps * 5` variant.
- Variant được cluster theo mean Jaro-Winkler similarity `>=0,85`.
- Cluster thắng dùng tuple chính xác `(cluster_size, max_confidence)`; representative là variant có
  `conf` lớn nhất trong cluster thắng.
- Length/format/known-plate mapping và Event/attribute publication giữ đúng thứ tự master. Không
  chèn best-result reducer, top-K winner hoặc custom consensus minimum vào decision.

**Face master contract:**

- Giữ `person_face_history[event_id]` gồm `(identity, score, face_area)`; tối đa 12 attempt khi chưa
  nhận diện và tối đa 6 attempt sau khi đã có recognition.
- `unknown` không tham gia weighted vote. Area weight bị cap ở `4000` rồi nhân
  `(score - unknown_score) * 10`.
- Identity thắng là tổng weighted score lớn nhất, nhưng chỉ hợp lệ khi count đạt `min_faces`; nếu
  identity khác có cùng count thì trả `(None, 0.0)`.
- Publish sub-label chỉ khi weighted score đạt `recognition_threshold`. `min_faces` phải hoạt động
  như master, không deprecated; top1-top2 margin và `BestResultReducer` không tham gia quyết định.

**Cách triển khai:**

1. Khóa master commit trong note/test evidence; lấy function-level diff cho bốn file chuẩn trước khi
   sửa.
2. Trả decision code/config về master theo từng hàm. Không checkout đè toàn file vì phải giữ
   Camera-specific trace/media instrumentation và dirty change ngoài recognition.
3. Bypass hoặc xóa khỏi production path `BestResultReducer`, custom Face pipeline decision,
   deprecated `min_faces`, custom LPR cluster tie-break và lifecycle gate làm thay master output.
4. Instrumentation còn lại chỉ ghi lại input/output của master function; không giữ evidence lease,
   trì hoãn publish hoặc thay đổi candidate order.
5. Thêm differential regression dùng cùng chuỗi observation cho local và frozen master behavior:
   LPR single variant, cluster tie, conflicting text và rolling-window prune; Face unknown,
   `min_faces`, count tie, weighted area/score và attempt limits.
6. Chạy runtime test một vòng, giữ toàn bộ trace/media/report. Report phải ghi master commit,
   function diff, voting history, representative/winner và mọi sai lệch quan sát được; recognition
   accuracy vẫn là evidence, không phải lý do sửa fixture hoặc master semantics.

**Giữ nguyên từ Phase 6-0:** finite-MP4 FIFO/backpressure, source-index timestamp, EOF synchronization,
raw tracker/Event ID, native clips, fixture plate-only và cấu trúc artifact theo trace. Restore
master không được sửa fixture, ghép trace hoặc thay model/threshold để làm đẹp kết quả.

#### Phase 6-2 — Typed recognition contract và core đồng bộ [DONE]

Mục tiêu là một package LPR/Face có thể import và test mà không khởi tạo detection subscriber,
Event, SQLite, API, notification hoặc recording. Tách theo boundary sau tracker, vì voting cần
track lifecycle ổn định; core không nhận raw detector batch và không tự tạo tracker.

- Tạo package `frigate/src/frigate/application/recognition/` với các module `contracts.py`, `core.py`, `lpr.py`,
  `face.py` và `ports.py`.
- `TrackedObservation` chứa `camera_id`, `stream_epoch`, caller-owned `track_id`, task, `frame_time`,
  object/detail bbox, `observed_in_frame`, attributes và opaque `EvidenceRef`.
- Lifecycle API tối thiểu là `observe()`, `end_track()` và `shutdown()`. Thiếu một observation không
  được tự suy ra track end; core không sinh passage ID, alias hoặc suffix.
- `RecognitionUpdate` giữ task/key, frame/evidence lineage, raw result, aggregate result, score và
  decision reason; không chứa Event mutation, database command hoặc notification command.
- Model runner, config reader, evidence resolver và observer/metrics đi qua protocol được inject.
  Core không import `DetectionSubscriber`, `EmbeddingMaintainer`, Event publisher hay filesystem
  trace writer.
- Chuyển LPR variant history/clustering và Face `person_face_history`/weighted voting đã khóa ở
  Phase 6-1 vào core theo từng hàm, chạy lại cùng differential vectors sau mỗi bước di chuyển.
- Phase 6 chỉ dùng lời gọi đồng bộ. Không thêm job queue, result queue, timeout hoặc retry transport
  vì các cơ chế đó có thể đổi observation order và master publication cadence.

#### Phase 6-3 — Frigate adapters và một production decision path [DONE: SOURCE/DEV]

- `FrigateRecognitionAdapter` map canonical tracked-object update/frame hiện có sang
  `TrackedObservation`; giữ nguyên raw Frigate track/Event ID và thứ tự callback.
- `FrigateEvidenceResolver` resolve shared-memory frame/crop cho core nhưng ownership/release phải
  rõ và idempotent. Video recording/clip vẫn thuộc Frigate Event/media lifecycle, không chuyển vào
  recognition core.
- `FrigateEventAdapter` là nơi duy nhất map `RecognitionUpdate` sang tracked-object update,
  sub-label, recognized-license-plate attribute và snapshot metadata. Adapter không vote hoặc chọn
  lại winner.
- Xóa production dependency vào `BestResultReducer`, `FaceRecognitionPipeline`,
  `PreparedPlateCandidate`, `PendingLprEligibility`, custom passage association và lifecycle/config
  key làm đổi master decision. Historical modules chỉ được giữ khi không còn production import.
- `EmbeddingMaintainer` chỉ orchestration adapter; không sở hữu recognition history, attempt budget,
  candidate rank hoặc terminal decision.
- Field `observed_in_frame` được truyền khi producer có bằng chứng. Không thêm heuristic suy đoán vào
  adapter; nếu stale bbox vẫn tái hiện sau master restore, ghi thành lỗi input contract riêng trước
  khi thay đổi admission.

#### Phase 6-4 — Cleanup và observability không phụ thuộc detection [DONE: SOURCE/DEV]

- Mỗi track session, model job và evidence handle có đúng một owner; `end_track()`/shutdown release
  idempotent. Sau drain phải có session/in-flight/pinned-evidence bằng 0 và không late publish.
- Core phát structured observer records qua port không blocking; không mở file, encode JPEG hoặc
  giữ image chỉ để trace.
- Frigate integration dùng bounded writer riêng cho JSONL và acceptance evidence. Queue đầy phải
  drop/count có kiểm soát; shutdown flush có deadline. Detection/recognition thread không trực tiếp
  `open()`, `write()`, `cv2.imencode()` hoặc chờ disk I/O.
- Metrics giữ master-relevant history/attempt/representative, processing latency, session depth,
  evidence ownership và trace writer depth/drop/error. Xóa metric chỉ mô tả custom Phase 5 như
  best-result rank, diversity budget hoặc custom terminal status.

#### Phase 6-5 — Differential, embedding và runtime evidence [DONE]

- Unit core chạy bằng fake model/evidence ports, không import hay khởi động Frigate detection/Event.
  Đây là bằng chứng recognition đã có thể nhúng như Python component.
- Differential suite đưa cùng ordered observations vào frozen master behavior và core: LPR single
  variant, cluster tie/conflict/window prune; Face unknown, `min_faces`, count tie, weighted area/
  score và attempt limits. Decision/update sequence phải giống nhau.
- Adapter integration test chứng minh canonical Frigate update → core → Event metadata giữ cùng raw
  track ID, frame/bbox/evidence và không duplicate publication.
- Chạy static/unit/integration, Ruff, `compileall`, `git diff --check`; sau đó build đúng một immutable
  image, lưu image digest cùng source/worktree hash.

Execution gate, thứ tự build/run và yêu cầu dùng `deploy/run.ps1` được quản lý tập trung tại
[AGENTS.md](../../AGENTS.md); tài liệu này chỉ ghi kết quả và artifact, không lặp lại rule.
- Runtime entrypoint chạy một vòng, giữ full report/summary/log/media/hardware metrics và master/core
  voting history. Báo cáo evidence-only, không dùng accuracy threshold để đổi kết quả; mọi giá trị
  PASS/MISSING/raw value được giữ nguyên.
- Phase 6 hoàn tất khi chỉ còn một production decision owner, core import/test độc lập detection và
  Event, Frigate adapter đạt differential/integration parity, terminal ownership về 0 và runtime
  artifact đầy đủ. LPR `11/11` không phải điều kiện đóng phase.

**Kết quả source/dev mới ngày 11/08/2026.** Package `frigate.recognition` cung cấp typed contract,
core đồng bộ, per-camera Frigate adapters, borrowed evidence và bounded trace writer. Canonical
tracked-object callback là production input duy nhất cho Face/LPR; core sở hữu history/attempt/vote,
Event adapter sở hữu publication, và maintainer chỉ gọi processor adapter/end/shutdown. Các module
Phase 5, deferred Face/LPR pipeline, untracked dedicated-LPR và disabled LPR post-processor đã bị
xóa; config dedicated LPR thiếu caller-owned `license_plate` track fail rõ.

Frozen-master/differential, standalone import, immutable lineage, duplicate/late suppression,
snapshot failure isolation và production source guard đã đạt. `ty check`, targeted Ruff,
`compileall` và `git diff --check` đạt. Dev restart bind-mounted đạt hai lần; finite-source prebuild
replay không còn watchdog `AttributeError`, container healthy/restart `0`, và final recognition là
`sessions=0`, `in_flight=0`, `evidence_pinned=0`, writer depth/drop/error `0`. Full config suite vẫn
bị chặn bởi outer test environment không đăng ký detector `cpu`; video/canonical integration suite
thiếu runtime dependencies `norfair`/SQLite extension, trong khi source-bound Docker runtime import
và health đã đạt.

Evidence cycle kế tiếp đã build đúng một lần image
`camera-frigate:overlay-41b5c90582b1`, digest
`sha256:41b5c90582b148df70220d3064311e89c2df5f0bd524c215e11cdf70c307d125`.
Runtime invocation duy nhất là
[`20260811-124202-033`](../../.tmp/platform-runtime/20260811-124202-033/report.md), dừng `exit 1`
sau `92,598 s`. Fixture contract và source PTS đạt, restore runtime đạt, nhưng
`measurement_valid=false`; report không có recognition/native-media/hardware result vì runtime LPR
evidence writer không quiescent. Container log chứng minh process `embeddings` exit code `1` và bị
watchdog restart; trace vì vậy chỉ có detector observations, không có canonical tracked-object
recognition stages.

Root cause là helper startup-only `__face_library_stats()` bị xóa nhầm trong migration trong khi
constructor vẫn gọi nó. `AttributeError` xảy ra khi ArcFace daemon model-builder còn sống nên process
chỉ lộ native abort `terminate called without an active exception`. Helper observability đã được khôi
phục và có regression test. Core source SHA-256 sau fix là
`5b65a574fb706a25bc91e8b032cfd27ee078520e06e9370f275fb1087151bb43`. Source-mounted dev restart
sau fix giữ nguyên embeddings PID `851` qua
`35 s`, không có restart/native abort, Docker healthy/restart `0`; stats cuối là `sessions=0`,
`in_flight=0`, `evidence_pinned=0`, writer depth/drop/error `0` và skipped FPS hai camera bằng `0`.
Fix này nằm sau immutable build nên digest trên không đại diện source cuối. Phase 6 giữ `[PARTIAL]`;
cycle sau phải build image mới và chạy đúng một runtime invocation mới để chứng minh Face/LPR trace,
native media, cleanup và hardware/report contract đầy đủ.

Evidence cycle được user cho phép tiếp theo khóa cùng core SHA-256, đạt `69` preflight tests,
`compileall` và `git diff --check`, rồi build đúng một lần image
`camera-frigate:overlay-e34dfad50ac1`, digest
`sha256:e34dfad50ac11ae03fbd26c021cf6eaa87e564ec5825d0b01bcc15ade72417ef`.
Runtime invocation duy nhất
[`20260811-145505-180`](../../.tmp/platform-runtime/20260811-145505-180/report.md) dừng `exit 1`
sau `114,454 s`: source PTS và restore đạt nhưng `measurement_valid=false`, không có accuracy,
native media hoặc hardware/cleanup result hợp lệ. Lỗi trực tiếp là adapter gọi
`lpr_process(..., raw_only=True)` trong khi mixin contract chỉ nhận ba positional arguments; process
embeddings vì vậy bị watchdog restart và LPR evidence writer không quiescent.

Call site đã được sửa theo exact mixin signature và có regression test model-port. Source-mounted
dev restart sau fix giữ nguyên embeddings PID `865` qua `35 s`, không có `raw_only`, traceback hoặc
restart; Docker healthy/restart `0` và session/in-flight/pinned/writer depth/drop/error đều `0`. Vì
fix nằm sau immutable build, Phase 6 tiếp tục `[PARTIAL]` và cần một evidence cycle mới.

Evidence cycle kế tiếp build image `camera-frigate:overlay-1a82f08dc07b`, digest
`sha256:1a82f08dc07b187c45c6b4fb70437d1383ba5a6c0662fbb2626f6a576c907863`, rồi chạy đúng một
runtime invocation
[`20260811-151758-676`](../../.tmp/platform-runtime/20260811-151758-676/report.md). Run `exit 0`,
report `complete`, source/restart/native-media contract đạt và giữ nguyên kết quả thực tế LPR
`7/11`; tuy nhiên terminal cleanup fail với `sessions=2`, `writer_depth=1`, còn `in_flight=0`,
`evidence_pinned=0`, drops/errors `0`. Root cause source-level là recognition chỉ được cleanup từ
finalized-event subscriber, quá muộn so với finite-source cutoff. Maintainer đã được sửa để canonical
tracked-object `end` gọi Face/LPR `expire_object()` trước frame lookup; finalized-event callback chỉ
còn là idempotent safety net. Regression test chứng minh end callback cleanup cả khi end-frame không
còn trong shared memory; source-mounted dev restart sau fix đạt cleanup zero.

Cleanup-fix cycle cuối build đúng một lần image `camera-frigate:overlay-314293cc30c6`, Docker digest
và image ID cùng là
`sha256:314293cc30c623aee28ae6d9d342aa572e9c02a92c4d0a714a8a9ec054146ead`. Runtime invocation
duy nhất là
[`20260811-153007-241`](../../.tmp/platform-runtime/20260811-153007-241/report.md), `exit 0` sau
`189,2 s`; report `complete`, source PTS/rounds đạt, restart delta `0`, 12/12 native clips, Face/LPR
runtime stages và hardware metrics đầy đủ. Raw result giữ nguyên: LPR exact `7/11`, Face fixture
score `0`; accuracy không phải phase gate. Writer không drop/error và không còn evidence lease hay
in-flight call, nhưng bounded drain vẫn kết thúc tại `sessions=2`, `writer_depth=1`, do hai raw
tracker cuối không phát explicit tracked-object `end` trước cutoff. Theo input contract của Phase 6,
core không được suy diễn `observed_in_frame`, tự timeout track, ghép passage hoặc tạo synthetic end.
Vì vậy artifact này được giữ nguyên làm failure evidence và Phase 6 vẫn `[PARTIAL]`; blocker còn lại
là caller/tracker phải phát explicit end cho mọi track trước terminal drain, sau đó mới được mở một
immutable build/runtime cycle mới để chứng minh cleanup zero.

Audit artifact của run này còn phát hiện trace
`lpr:car_camera:1786437065.963994-ydtdxn` có đầy đủ `track_seen`, plate detection, ba OCR
`FKH9211`, publication và native clip nhưng không có JPEG evidence trong trace folder. Nguyên nhân
không phải tracker: quota `PASSAGE_EVIDENCE_MAX_BYTES=128 MiB` đã bị tính theo `image.nbytes` của
raw ndarray trước JPEG, làm các trace xuất hiện muộn bị `artifact_rejected=byte_limit` dù tổng JPEG
đã ghi chỉ khoảng `8,94 MiB`. Writer đã được sửa để mọi JSONL stage record vẫn được ghi và byte
quota chỉ tính trên JPEG thực tế trong writer thread; bounded queue/`put_nowait` tiếp tục giới hạn
RAM và không block recognition. Regression khóa cả encoded-byte enforcement lẫn trường hợp raw
frame lớn/JPEG nhỏ; `29` recognition/parity/trace tests, `44` outer acceptance tests, Ruff, ty,
compileall và diff check đạt. Source-mounted dev restart giữ nguyên embeddings PID, Docker healthy,
restart `0`, skipped FPS hai camera và toàn bộ recognition depth/drop/error bằng `0`. Fix evidence
này nằm sau image/run trên, nên vẫn cần immutable runtime cycle mới để chứng minh mọi raw trace có
đủ record/media và không còn `byte_limit` starvation.

Immutable evidence cycle cho fix này build đúng một lần image
`camera-frigate:overlay-1b78b78bf33b`, digest
`sha256:1b78b78bf33bb52904c5823a810928f806fa36c0dab606738ace08d58120c326`, rồi chạy đúng một
invocation
[`20260811-155236-940`](../../.tmp/platform-runtime/20260811-155236-940/report.md). Run `exit 0`,
report `complete`, source PTS/rounds đạt, restart delta `0`, skipped FPS hai camera `0`, native clips
12/12 và runtime evidence `valid=true`. Manifest có `162` JPEG/`19.530.214` bytes, không có
`artifact_rejected`, missing path, writer drop hoặc writer error; cả 11 raw LPR trace đều có ảnh.
Trace tương ứng fixture `FKH9211` có 8 ảnh gồm object bbox, car crop, plate detector input, plate
crop, OCR plate input, OCR text crop và recognition tensor, cùng native clip/trace JSON. Như vậy lỗi
late-trace evidence starvation đã đóng. Phase vẫn chưa `[DONE]` vì terminal cleanup độc lập còn
`sessions=2` dù `in_flight=0`, pinned evidence `0` và writer depth `0`.

Evidence cuối sau khi khóa fixture Face theo yêu cầu vận hành nằm tại
[`20260811-201337-397`](../../.tmp/platform-runtime/20260811-201337-397/report.md). Builder giữ nguyên
file gốc `01_P1E_S1_C1.mp4`; runtime dùng trực tiếp clip cố định `01_P1E_S1_C1_5s-20s.mp4` đã cắt
từ giây 5 đến giây 20 và lưu cạnh file gốc. Fixture không còn logic cắt/chuyển mã lúc chạy. Clip mới
dài `15,000000 s`, 225 frame ở 15 FPS. Face library được snapshot
read-only gồm `15` identity/`56` ảnh, không copy `train` và không tạo synthetic enrollment. Runtime
đọc đúng `4` raw Face trace tương ứng bốn lượt người trong đoạn 15 giây, cả `4/4` có recognition
(`1 Joe`, `1 Daniel`, `2 unknown`, không có trace `not_recognized`), đồng thời sinh `22/22` ảnh
annotated có person/face bbox mà không đổi hash JPEG raw. Canonical callback gắn explicit
`observed_in_frame`; observation hợp lệ mượn one-shot shared-memory evidence handle để tên frame
ring không bị tái sử dụng trước inference, rồi embeddings consumer xóa handle ngay sau lời gọi đồng
bộ. Evidence của người áo sọc tại source time khoảng `9,2 s` vì vậy giữ đúng người/bbox và trả
`unknown`, không còn crop nhầm frame Joe ở khoảng `12 s`. LPR vẫn đọc đúng `11/11` raw trace; kết
quả exact thực tế là `6/11`, không dùng threshold accuracy để đóng Phase 6. Native clip đạt `15/15`,
writer drop/error bằng `0`, restart bằng `0`, và
cleanup cuối đạt `sessions=0`, `in_flight=0`, `evidence_pinned=0`, `writer_depth=0`. Report cuối
`complete`, `measurement_valid=true`, `runtime_restored=true`; Docker sau restore được kiểm tra
`running healthy`, restart `0`.

### Phase 7 — External recognition runtime [DONE]

Phase 7 triển khai recognition boundary: model inference, `RecognitionCore` và Face/LPR session
state có thể chạy trong `camera-recognition`, còn Frigate giữ host integration, evidence contract,
Event/media publication. Tracker vẫn nằm trong Frigate ở runtime hiện tại; việc tách tracker thành
edge boundary là kiến trúc đích tiếp theo và không cho phép tracker gọi recognition trực tiếp.

#### Phase 7-1 — Ordered executor và transport-neutral contract [DONE: SOURCE/UNIT]

- Thêm `RecognitionJob`, `JobReceipt`, `RecognitionOutcome` và bounded executor một partition quanh
  synchronous core. Observation admission dùng `put_nowait`; queue đầy trả `queue_full`, không block,
  không loại job cũ và không gọi synchronous path.
- Giữ sequence theo `TrackKey`; dành control capacity cho `end_track`; deadline/cancel chỉ discard
  late update, không tuyên bố dừng model call đang chạy. Differential test phải khớp synchronous
  update sequence khi không overload.
- Default: một partition, 128 observation slots, 64 active-session/control slots, 128 outcomes,
  RPC deadline 5 giây, accepted-job deadline 30 giây và shutdown drain 10 giây. Capacity được cấu
  hình nhưng reject semantics không đổi.

#### Phase 7-2 — gRPC/mTLS recognition service [DONE: SOURCE/UNIT]

- Dựng container riêng chứa Face/LPR model adapters, executor, core, standard health,
  `GetCapabilities`, config/Face control RPC và bidirectional recognition stream. Mỗi start tạo
  `service_epoch`; dedupe tối đa 4.096 outcome trong 60 giây theo `client_id + job_id`.
- Chỉ dùng gRPC cho production external path. Raw I420 evidence có metadata/TTL và hard limit
  8 MiB; không ZeroMQ production transport, shared IPC namespace, URL/path resolver hoặc plaintext
  non-loopback binding.

#### Phase 7-3 — Frigate runtime integration [DONE: SOURCE/UNIT]

- Sửa trực tiếp `EmbeddingProcess`/Face/LPR runtime để external mode không khởi tạo local inference
  model. Host client tạo ordered jobs, forward Face control operation, kiểm tra epoch/sequence/
  idempotency và chuyển update hợp lệ duy nhất sang `FrigateEventAdapter` hiện hành.
- Config chỉ chọn `local` hoặc `external`. Đây là hai topology độc lập: external startup validation
  fail khi endpoint/TLS/config sai; runtime disconnect trả unhealthy/typed failure và không fallback,
  tự retry job hoặc nối session qua epoch.

#### Phase 7-4 — Docker acceptance và packaging [DONE]

- `deploy/run.ps1 build` đã tạo Frigate overlay và recognition overlay riêng từ cùng source tree;
  service có GPU/resource/health policy, private network, Face library và certificate mount.
- Differential test với cùng ordered observations đã khóa cùng decision/update sequence giữa
  synchronous core và executor/transport khi healthy. Hai E2E độc lập cùng đọc 4 Face lineage
  (`3 known + 1 unknown`) và 11 raw LPR lineage; attempt count có thể khác do tracker scheduling,
  không được dùng hai wall-clock replay độc lập làm bit-exact differential.
- External healthy run mới `20260812-141524-728` complete với `measurement_valid=true`, Face
  lineage `4/4` (coverage `1.0`, `38` track-seen), raw LPR `11/11`, API/SQLite consistency,
  correlation mismatch `0`, không reconnect/stall, service healthy, external không load local model,
  cleanup/pending/writer về `0` và restore thành công. LPR exact `7/11` được giữ nguyên là
  diagnostic, không phải hard gate.
- Validator hiện tương quan bằng producer-owned `trace_id`, source PTS, Event id và media lineage;
  fixture passage/time/bbox chỉ là comparison view. External trace writer đã bổ sung lineage/geometry
  cho update từ service. `measurement_valid` vẫn tách khỏi Face/LPR accuracy.
- Đã thêm `tools/tests/e2e/run_external_recognition_fault_test.py --scenario ...` cho ba fault
  scenario và `tools/package_recognition_wheels.py`. Hai wheel được build reproducible trong
  `.tmp/recognition-wheels`, có source commit/worktree hash và SHA-256; clean Python 3.11
  install/import đã pass. Lần build thứ hai trong `.tmp/recognition-wheels-rebuild` khớp
  SHA-256 và byte size.
- Diagnostic LPR accuracy vẫn giữ nguyên kết quả khó đọc và không chặn phase. Ba fault artifact
  đều hoàn tất qua validator: `stream_disconnect` (`20260812-130708-272`), `client_disconnect`
  (`20260812-132857-884`) và `service_restart` (`20260812-135955-917`). Typed interruption
  không còn bị validator đánh nhầm là healthy attempt; cleanup/restore và publication safety là
  hard gate. Vì vậy Phase 7-4 và Phase 7 được đóng `[DONE]`.

Phase 6-0 hoàn tất riêng không đồng nghĩa recognition core đã hoàn tất. Hiện Phase 6-1…6-5 đều đã
hoàn thành và Phase 6 đã đóng `[DONE]`. Phase 7 được đánh giá độc lập sau đó; `Acceptance tổng thể`
vẫn kết luận riêng theo mục 14 trên artifact của toàn bộ roadmap đã chọn triển khai.

## 14. Acceptance tổng thể

Acceptance là kết luận xuyên phase cho toàn bộ kiến trúc sau khi roadmap hoàn thành. Nó không thay
implementation plan, không phải tên khác của trạng thái Phase 5 và không dùng một con số accuracy
chung cho mọi camera. Trạng thái `[DONE]` xác nhận phạm vi implementation của phase đã hoàn thành;
kết quả đo của phase vẫn được giữ nguyên làm dữ liệu đầu vào cho kết luận Acceptance tổng thể.

Hệ thống chỉ được ghi `Accepted` khi tất cả phase trong roadmap đã `[DONE]` và các gate toàn cục
dưới đây đạt trên artifact công bố. Trước thời điểm đó, tài liệu chỉ báo trạng thái từng phase và
kết quả đo hiện tại, không suy diễn Acceptance từ riêng Phase 5 hoặc một quick run.

| Loại gate | Quy tắc |
| --- | --- |
| CV quality | Passage recall/precision, recognition precision/recall và latency đạt product profile trên denominator công bố; không mặc định 100% |
| Data correctness | Không gắn plate/identity sang physical passage khác; commit, frame reference, bbox và evidence cùng generation |
| Runtime correctness | Queue/memory bounded, không stale result, duplicate owner hoặc duplicate commit; external failure trả typed outcome và không fallback |
| Development acceptance | Unit/integration/replay case độc lập hoàn thành dưới 120 giây để giữ vòng lặp phát triển ngắn |

## 15. Bản đồ code và đường dẫn cần can thiệp

Đường dẫn tính từ repository root. Trạng thái mô tả code hiện tại, không thay cho checkbox
acceptance ở mục 13:

- `[DONE]`: phạm vi implementation của phase/component đã hoàn thành và có bằng chứng tương ứng.
- `[NEXT]`: phase/component kế tiếp đã khóa nguồn chuẩn và scope nhưng chưa triển khai.
- `[PLANNED]`: phase sau đã có boundary nhưng chỉ triển khai khi dependency trước hoàn tất.
- `[TODO]`: file đã tồn tại nhưng hạng mục chưa hoàn thành.
- `[PARTIAL]`: đã có một phần logic và phải mở rộng, không viết lại từ đầu.
- `[NEW]`: đường dẫn chưa tồn tại và sẽ được tạo ở phase tương ứng.
- `[CURRENT]`: entrypoint/artifact đang được dùng xuyên roadmap.
- `[SUPERSEDED]`: implementation cũ đã bị thay thế và không còn là production contract.
- `[KEEP]`: phần hạ tầng trung lập được giữ lại nhưng không được tác động recognition decision.
- `[HISTORICAL]`: chỉ còn giá trị truy nguyên artifact/test cũ.
- `[OPEN]`: lỗi hoặc invariant còn tồn tại và đã xác định đúng owner cần sửa.

### 15.1 Phase 1 — Baseline và bằng chứng

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[DONE]` | `deploy/config.yaml`, `deploy/run.ps1` | `-ConfigFile` chạy config fixture cô lập; config mặc định không đổi. |
| `[DONE]` | `frigate/src/frigate/stats/prometheus.py`, `frigate/src/frigate/stats/util.py` | Camera/detector/enrichment metrics hiện có đủ cho baseline; face pending được lấy từ structured pipeline log. |
| `[DONE]` | `tools/fixtures/prepare_baseline_fixture.py`, `tools/runtime/validate_face_replay.py`, `tools/runtime/validate_lpr_acceptance.py`, `tools/reporting/summarize_baseline.py` | Fixture, ground truth, runtime gate và summary đều có hard budget dưới 120 giây. |

### 15.2 Phase 2 — Passage bottleneck

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[DONE]` | `frigate/src/frigate/domain/video/detect.py`, `frigate/src/frigate/infrastructure/data_processing/common/license_plate/mixin.py`, `frigate/src/frigate/util/passage_trace.py` | Instrument detector/track/eligibility/plate/OCR/Event funnel và sửa motion calibration, first-frame eligibility cùng crop context ở đúng tầng passage. |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/real_time/face.py`, `frigate/src/frigate/util/face_snapshot.py` | Trace theo track generation, reset close-follow và giảm thời gian từ passage đến confirmed bằng cadence/selection hiện có; chưa thêm shared quality/top-K. |
| `[DONE]` | `tools/fixtures/platform_passage_ground_truth.yaml`, `tools/fixtures/prepare_passage_fixture.py`, `tools/runtime/validate_platform_runtime.py`, `tools/tests/unit/test_passage_acceptance.py` | MP4 gốc được đưa qua capture/detect/track Frigate; raw tracker tạo trace. Fixture chỉ đối chiếu terminal plate, không giữ time/bbox và không có tracker/passage registry riêng trong test. |

### 15.3 Phase 3 — LPR execution foundation

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/common/license_plate/pipeline.py` | Typed track key, observation, prepared candidate và bounded track state được đặt trong module pipeline thực tế; không còn tham chiếu file `state.py` không tồn tại. |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/common/license_plate/mixin.py` | Plate inference được tách khỏi Event publish; observation đi qua temporal decision trước commit. |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/real_time/license_plate.py`, `frigate/src/frigate/infrastructure/data_processing/real_time/api.py` | Foundation worker/stale guard là artifact lịch sử Phase 3; production recognition decision phải theo master sau Phase 6-1. |
| `[DONE]` | `frigate/src/frigate/application/embeddings/maintainer.py` | Foundation dispatch/drain là artifact lịch sử; không trao quyền decision owner trái master. |
| `[DONE]` | `frigate/tests/test_lpr_track_state.py`, `frigate/tests/test_lpr_deferred_processor.py` | State lifecycle, unique-frame consensus, stale generation, queue replacement và one-decision/one-commit có regression test. |

### 15.4 Phase 4 — Camera quality, evidence và candidate contract

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/common/evidence.py` | `FrameRef`, `EvidenceLease`, `EvidenceCandidate`, ownership/expiry contract và bounded ring buffer dùng chung. |
| `[HISTORICAL]` | `frigate/src/frigate/infrastructure/data_processing/common/quality.py` | `QualitySelector`/top-K là artifact Phase 4; Phase 6-1 chỉ giữ diagnostic/evidence use, không giữ recognition decision behavior. |
| `[DONE]` | `frigate/src/frigate/infrastructure/config/camera/quality.py`, `frigate/src/frigate/infrastructure/config/camera/camera.py`, `frigate/src/frigate/infrastructure/config/config.py` | Camera quality profile và validation source role, FPS, byte budget cùng detect resolution. |
| `[HISTORICAL]` | `frigate/src/frigate/application/embeddings/maintainer.py` | Ring/selector lifecycle là artifact Phase 4; không được giữ quyền admission/decision cạnh tranh với master. |
| `[HISTORICAL]` | `frigate/src/frigate/infrastructure/data_processing/real_time/face.py`, `frigate/src/frigate/infrastructure/data_processing/real_time/license_plate.py` | Adapter `EvidenceCandidate` chỉ được giữ nếu hoàn toàn side-channel và parity-neutral. |
| `[DONE]` | `frigate/tests/test_evidence_quality.py`, các test Face/LPR deferred/state/snapshot | Kiểm tra ownership, bounds, replacement, stale generation và lineage; threshold accuracy thuộc product-profile calibration. |

### 15.5 Phase 5 — Custom recognition lifecycle [SUPERSEDED]

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[SUPERSEDED]` | Recognition/Face/LPR custom lifecycle, association, scheduler và config | Không còn là kiến trúc chuẩn; Phase 6-1 bypass hoặc xóa decision behavior lệch master. |
| `[KEEP]` | `frigate/src/frigate/util/passage_trace.py`, runtime report và native media instrumentation | Chỉ giữ side-channel quan sát; không được tham gia admission, voting, winner hoặc publish timing. |
| `[HISTORICAL]` | Phase 5 tests và artifacts | Chỉ dùng truy nguyên thử nghiệm cũ; không dùng làm parity source hoặc production contract. |

### 15.6 Phase 6 — Master-compatible standalone recognition core [DONE]

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[DONE]` | `frigate/src/frigate/domain/video/ffmpeg.py`, `frigate/tests/test_video.py` | Phase 6-0: finite MP4 dùng FIFO/backpressure và source-index timestamp; live/network source vẫn latest-only. Producer ghi start/EOF marker theo camera. |
| `[DONE]` | `tools/runtime/validate_platform_runtime.py`, `tools/tests/unit/test_passage_acceptance.py` | Phase 6-0: một invocation/một vòng; chờ cả producer EOF và processed final timestamp trước cutoff. LPR dùng raw tracker trace, fixture chỉ plate-only compare. |
| `[DONE]` | `.tmp/platform-runtime/20260811-044205-810/` | Phase 6-0: report evidence-only hoàn tất trong 129,302 s; 11 raw LPR trace, 11/11 LPR clip, skipped FPS LPR bằng 0 và runtime được restore. |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/common/license_plate/mixin.py`, `frigate/src/frigate/infrastructure/data_processing/real_time/license_plate.py` | Phase 6-1: exact master LPR variant window, Jaro cluster, `(size, max_conf)` winner và highest-conf representative. |
| `[DONE]` | `frigate/src/frigate/infrastructure/data_processing/real_time/face.py`, `frigate/src/frigate/infrastructure/config/classification.py` | Phase 6-1: master `person_face_history`, weighted voting, active `min_faces`, count-tie rejection và attempt limits. |
| `[DONE]` | Frozen master Face/LPR differential fixtures | Phase 6-1: function-level parity được khóa trước khi extract core. |
| `[DONE]` | `frigate/src/frigate/application/recognition/contracts.py`, `core.py`, `ports.py` | Phase 6-2: typed tracked observation, explicit track lifecycle, evidence/model/observer ports và typed update; import độc lập detection/Event. |
| `[DONE]` | `frigate/src/frigate/application/recognition/lpr.py`, `face.py` | Phase 6-2: synchronous master-compatible task engines và session state. |
| `[DONE]` | `frigate/src/frigate/application/recognition/adapters/frigate.py` | Phase 6-3: map tracked-object/frame vào core và map update về Event metadata, không decision logic. |
| `[DONE: SOURCE/DEV]` | `frigate/src/frigate/application/embeddings/maintainer.py`, Face/LPR realtime modules | Phase 6-3: orchestration/adapters duy nhất; custom reducer/pipeline/retry đã xóa khỏi production. |
| `[DONE]` | Face/LPR evidence ownership và raw bbox lineage | Runtime giữ raw trace ID, raw JPEG và native clip; ảnh bbox là artifact do chính producer tạo tại bước recognition, validator chỉ kiểm tra/copy và không dựng record giả hoặc vẽ lại media. |
| `[DONE]` | Recognition ownership, `passage_trace.py`, acceptance evidence và stats | Phase 6-4: idempotent cleanup, bounded JSONL/JPEG writer và master-relevant metrics; run cuối đạt sessions/in-flight/pinned/writer depth bằng `0`. |
| `[DONE]` | Core/differential/adapter tests, whole project và deployment | Phase 6-5: parity/import-isolation/single-owner tests đạt; run `20260811-201337-397` report complete, measurement valid, 4/4 Face raw traces recognized, 15/15 native clips, 22/22 bbox images và runtime healthy sau restore. |
| `[CURRENT]` | `tools/runtime/validate_platform_runtime.py`, `tools/reporting/summarize_platform_runtime.py`, Phase artifacts | Runtime test xuyên roadmap, full evidence report và diagnostic summary; không kết luận pass/fail bằng threshold. |

### 15.7 Phase 7 — External recognition runtime [DONE]

| Trạng thái | Đường dẫn | Can thiệp |
| --- | --- | --- |
| `[DONE: SOURCE/UNIT]` | `frigate/src/frigate/application/recognition/executor.py`, transport-neutral contracts | Bounded one-partition executor, ordered track lifecycle, typed receipt/outcome, deadline/cancel và terminal drain; không voting/winner/Event logic. |
| `[DONE: SOURCE/UNIT]` | `frigate/src/frigate/application/recognition/service/`, protobuf schema | gRPC/mTLS stream, health/capabilities, config và Face control operations, service epoch, bounded dedupe và raw I420 validation đã có targeted test. |
| `[DONE: SOURCE/UNIT]` | `frigate/src/frigate/infrastructure/data_processing/common/face_pipeline.py`, LPR mixin, `RecognitionCore` | Local và external dùng chung detector, crop, bbox/evidence producer, LPR processing, voting và publication decision; targeted differential/lifecycle tests đạt. |
| `[DONE: SOURCE/UNIT]` | `frigate/src/frigate/application/embeddings/maintainer.py`, `external_recognition.py` | External client được chọn trực tiếp trong runtime; nhánh này không khởi tạo local Face/LPR model, guard epoch/sequence trước publication, giữ accepted outcome đến ordered END và không có fallback. |
| `[DONE]` | Recognition Docker image, Compose/deployment và integration tests | Healthy external run `20260812-141524-728` đạt `measurement_valid=true`, Face `4/4`, raw LPR `11/11`, API/SQLite consistency, correlation `0`, không reconnect/stall, service healthy, local model `0`, cleanup/pending/writer `0`, restore thành công; ba fault artifacts và reproducible wheel manifest đã pass. LPR exact `7/11` là diagnostic. |
| `[DONE]` | Core/client wheel packaging | Hai wheel core/client đã được build sau runtime acceptance, clean-install/import và reproducibility pass; manifest ghi source/worktree hash, SHA-256 và byte size. |

### 15.8 Phase 8 — Edge tracker runtime [DONE — HEALTHY LOCAL E2E]

| Trạng thái | Đường dẫn | Bằng chứng/phạm vi |
| --- | --- | --- |
| `[DONE: SOURCE/CONTRACT]` | `frigate/src/extension/tracker/`, `frigate/src/frigate/domain/video/`, `frigate/src/frigate/domain/track/` | Typed contract, durable producer runtime và adapter dùng lại `CameraState`/`TrackedObject`/Norfair/PTZ gốc; ordered lifecycle và edge media chạy trong topology thật. |
| `[DONE: HOST OWNERSHIP]` | Frigate tracked-object host adapter, config schema, migration 040 | Camera owner, epoch/sequence/idempotency, evidence lineage, canonical Event/API/SQLite và recognition routing giữ tại Frigate main. |
| `[DONE: DEPLOYMENT]` | `deploy/run.ps1`, `deploy/config.yaml`, `deploy/reference/docker-compose.yml` | Launcher tạo đồng thời Frigate/recognition/tracker, bounded readiness trước input, source hot mount không build trong development và restore topology thành công. |
| `[DONE: HEALTHY E2E]` | `tools/tests/e2e/run_platform_runtime_test.py`, `tools/runtime/validate_platform_runtime.py` | Run `20260814-221312-066` pass toàn bộ hard gate, đủ 4 Face + 11 LPR producer trace, 15 clip/trace, mismatch `0`, cleanup/pending `0`, restore `true`. |
| `[DEFERRED: PHASE 9]` | Remote/fault campaign | Tracker restart, network disconnect và remote multi-host recovery không được tuyên bố pass trong Phase 8; chuyển sang acceptance của Phase 9. |

### 15.9 Phase 9 — Remote distributed runtime [PLANNED]

| Trạng thái | Đường dẫn | Phạm vi sau Phase 8 |
| --- | --- | --- |
| `[PLANNED]` | `deploy/run.ps1`, `deploy/config.yaml`, deployment manifests | Remote endpoint/topology cho cả tracker và recognition; không quản lý container remote như local |
| `[PLANNED]` | Tracker/recognition transport, config và host adapters | gRPC/mTLS preflight, node/camera ownership, schema/config hash, tracker epoch và recognition epoch |
| `[PLANNED]` | `tools/tests/e2e/`, `tools/runtime/validate_platform_runtime.py` | Remote end-to-end healthy/fault evidence trên cả hai link, publication safety, cleanup và restore |
| `[PLANNED]` | `tools/tests/README.md`, canonical runtime validator | Report remote topology, certificate/network evidence, per-node resource và source/topology hash |

### 15.10 Profile nguồn LPR 1024p hiện tại

`car_camera` dùng file nguồn đã chuẩn hóa `1820×1024`, detect `1820×1024/5 FPS`;
`face_camera` là `1280×720/15 FPS` với `max_disappeared=15` để giữ lifetime tracker đúng một giây. Fixture builder không còn dùng một hằng số
1280×720 chung mà đọc frame theo từng pipeline. Ground-truth LPR không còn bbox/ROI/time;
validator chỉ so final published plate sau khi pipeline đã tạo trace độc lập.
Replay acceptance LPR cũng được chuẩn hóa về 5 FPS thay vì phát 15 FPS vào detect 5 FPS,
tránh tạo RTSP buffer không thuộc contract và làm sai round anchor khi chạy 1024p.

Model object detection vẫn nhận tensor 320×320 và recognition model, threshold,
Event/API/SQLite contract không đổi. Các quality gate `min_area`, kích thước detail và
edge clearance vẫn là pixel gate trên detect frame, vì vậy ảnh 1024p chủ động cung cấp
nhiều chi tiết hơn thay vì scale threshold để triệt tiêu lợi ích đó. Evidence vẫn bị chặn cứng ở
32 MiB/camera: một I420 frame 1820×1024 chiếm 2.795.520 byte và vòng đệm sẽ loại frame cũ chưa pin
trước khi vượt ngân sách. Số frame giữ lại không mang semantics top-K hoặc recognition attempt.

## 16. Tài liệu liên quan

- [Use case và so sánh chi phí Dahua/Hikvision/LS CV](../CameraUseCase.md)
- [Kiến trúc ADAS Level 0](ADAS.md)
- [Dahua IPC HTTP API](../references/DahuaHTTPAPI.pdf)

## 17. Báo cáo hiện trạng runtime — 14/08/2026

Artifact xác nhận: [tracker E2E `20260814-221312-066`](../../.tmp/platform-runtime/20260814-221312-066/report.md).

| Nhóm | Kết quả |
| --- | --- |
| Healthy tracker E2E | `accepted=true`; `acceptance.status=passed`; `measurement_valid=true`; restore thành công |
| Face | `4/4` lineage, coverage `1.0`, `32` track-seen, `29` attempts |
| Car/LPR | `11/11` raw trace, `7` publication; exact `5/11` giữ nguyên là diagnostic |
| Tracker media | 15 producer event; đủ 15 `clip.mp4` và 15 `trace.json` trong report |
| Data correctness | API/SQLite pass; correlation mismatch `0` |
| Runtime stability | Bad runtime log/restart/pending `0`; recognition cleanup, writer drop và writer error đều `0` |
| Resource peak | GPU `37%`; VRAM `1099/4096 MiB`; shared memory `3%`; RAM `4518930545` byte |
| CPU | Peak Docker tổng các container `989,03%`; đây là số cộng trên nhiều logical core |
| E2E elapsed | `104,265` giây nội bộ, `106,026` giây theo host; replay `11,172` giây |

Diễn giải CPU: phần trăm là Docker CPU cộng theo các container, nên `100%` xấp xỉ một logical
core. Peak startup không đại diện tải duy trì; artifact không cho thấy queue tích lũy, stall hoặc
OOM trong healthy run. Phase 8 local edge tracker đã `[DONE — HEALTHY LOCAL E2E]`; Phase 9 remote
distributed runtime cho cả tracker và recognition vẫn `[PLANNED]`. LPR exact thấp tiếp tục được giữ
nguyên dưới dạng diagnostic, không dùng để phủ nhận hoặc tô đẹp closure kiến trúc của Phase 8.
