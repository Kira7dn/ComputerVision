# Bàn giao điều tra và ổn định DeepStream Camera

Ngày: 20/08/2026  
Workspace: `D:\BusinessAnalyze\Camera`  
Runtime đang áp dụng: standalone WSL DeepStream trong `deepstream_safety/`

## 0. Mục tiêu bắt buộc của phiên phát triển tiếp theo

Phiên tiếp theo là **implementation session**, không phải phiên chạy lại acceptance
cho các sửa cũ.

Mục tiêu duy nhất: triển khai person confirmation gate tối thiểu trong application
tracker hiện tại để raw person detection chập chờn trên thùng rác
`camera_safety` không được công bố, đồng thời person đã xác nhận không nháy khi
detector hụt ngắn. Patch này không tuyên bố xử lý ghế hoặc thùng rác bị model
nhận liên tục; các trường hợp persistent đó thuộc patch per-camera calibration
kế tiếp.

Baseline đã kiểm tra tại `main` commit `2b4fcbc`:

- Worktree sạch.
- Tracker edge-transition, mock RTSP readiness và smoking overlay ownership đã có
  trong HEAD; chỉ cần giữ regression, không lấy việc kiểm tra lại chúng làm mục
  tiêu phiên.
- Person confirmation gate **chưa có**. Các `confirmation_hits` hiện có trong
  `config.yaml` thuộc smoking và fire/smoke, không xác nhận person track.

Source bắt buộc phải thay đổi trong phiên:

- `deepstream_safety/pipeline.py`: thêm `confirmed`, `hit_times`, `last_seen_at`
  vào `_person_tracks`; filter consumer theo confirmed/current/render view.
- `deepstream_safety/tracking.py`: thêm helper time-window hit nhỏ, không tạo
  tracker framework mới.
- `deepstream_safety/config.yaml` và `config.py`: thêm đúng ba cấu hình
  `confirmation_hits`, `confirmation_window_seconds`, `render_grace_seconds`.
- `tools/tests/unit/test_deepstream_tracking.py`: thêm bốn regression case đã chốt.

Kết thúc phiên bằng targeted unit/static checks và một runtime acceptance chạy
đúng 30 giây. Không thêm soak test, không chỉ restart/xem dashboard, không đổi
Frigate/model/topology và không gọi phiên hoàn tất khi source person confirmation
gate chưa được triển khai.

## 1. Mục tiêu và ranh giới

Tài liệu này bàn giao kết quả điều tra các hiện tượng live không ổn định, bbox
nháy giữa `person` và `SMOKING`, mất event do `track_id` không liên tục, mock RTSP
khởi động thất bại, cùng các false positive person/fire quan sát được trên
`camera_dahua` và `camera_safety`.

Kiến trúc runtime không được thay trong đợt sửa:

```text
config.yaml
  -> multi_runner.py
  -> một pipeline.py process cho mỗi camera
  -> person detector + application tracker
  -> person ROI smoking classifier / full-frame fire-smoke detector
  -> temporal state + event/evidence + notification
  -> NVOSD -> MediaMTX RTSP/HLS -> dashboard
```

Nested `frigate/`, Docker và Frigate API không tham gia runtime này.

### 1.1 Kiến trúc duy nhất

Không có nhiều phương án kiến trúc trong tài liệu này. Bảng dưới đây là nguồn sự
thật duy nhất; `CURRENT` là phần đang chạy trong HEAD, `TARGET` là thay đổi đã chốt
nhưng chưa triển khai.

| ID | Trạng thái | Quyết định kiến trúc | Liên quan |
| --- | --- | --- | --- |
| `A-01` | **CURRENT — giữ nguyên** | Runtime chỉ là `config.yaml -> multi_runner.py -> một pipeline.py worker/camera -> MediaMTX -> dashboard` | Toàn hệ thống |
| `A-02` | **CURRENT — giữ nguyên** | Mỗi camera worker sở hữu inference, application tracker, annotation và function dispatch của camera đó | Toàn hệ thống |
| `A-03` | **CURRENT — giữ nguyên** | `EvidenceStore` là owner duy nhất của event trace, SQLite idempotency và annotated snapshot dưới `.tmp/deepstream-safety` | Event/evidence |
| `A-04` | **TARGET — phiên hiện tại** | Application tracker hiện có vẫn là authority duy nhất cấp person `track_id`; chỉ thêm `confirmed`, `hit_times`, `last_seen_at`, confirmation gate và render grace | `I-06` |
| `A-05` | **TARGET** | Một camera worker có đúng hai bounded latest-sample analysis lane: person behavior và environmental fire/smoke; không tạo queue history | `I-08` |
| `A-06` | **TARGET** | Output GStreamer dùng latest-frame/leaky queue; dashboard HLS đo freshness và tự seek/recreate khi vượt hard limit | `I-09` |
| `A-07` | **TARGET** | WSL systemd sở hữu vòng đời `multi_runner.py`; PowerShell launcher chỉ gọi start/stop/status | `I-10` |
| `A-08` | **TARGET** | Threshold và ROI của person/fire/smoke nằm trong camera profile; không dùng một global policy để che khác biệt camera | `I-05`, `I-07` |
| `A-09` | **CURRENT — policy cố định** | Danh sách event giữ đầy đủ event chức năng; Telegram/Zalo chỉ nhận fire, smoke và smoking | Notification |

Luồng target duy nhất sau khi hoàn thành roadmap:

```text
config.yaml
  -> multi_runner.py
  -> one pipeline.py worker per camera
       -> person detector
       -> existing application tracker + confirmation gate
       -> confirmed current person -> face/smoking lane
       -> full frame -> fire/smoke lane
       -> event/evidence + notification policy
       -> canonical annotation
       -> bounded latest-frame RTSP output
  -> MediaMTX HLS
  -> dashboard freshness watchdog
```

Ràng buộc không được thay đổi:

- Không đưa Frigate, Docker, `/media/frigate` hoặc Frigate API vào runtime.
- Không thêm NvDCF, tracker service, event bus hoặc media owner thứ hai.
- Không chạy inference trên bbox render grace/cache.
- Không để frontend trở thành owner của detection/event; frontend chỉ render
  canonical metadata và giám sát freshness.
- Mỗi patch chỉ triển khai đúng một kiến trúc `TARGET` theo thứ tự mục 8.

### 1.2 Contract bắt buộc cho `I-06`

Phần này trả lời dứt điểm các câu hỏi triển khai; không có lựa chọn khác.

**Bằng chứng xác nhận person candidate**

- Confirmation chỉ dùng temporal detection hits: cùng track có ít nhất
  `confirmation_hits: 3` detection match trong
  `confirmation_window_seconds: 1.5`.
- Mỗi hit đã phải vượt `person.confidence` và được association vào cùng track bằng
  geometry matcher hiện có.
- Motion không tham gia confirmation.
- Không thêm geometry gate, score threshold thứ hai, person verifier hoặc model
  mới trong `I-06`.
- Bbox geometry chỉ dùng để association, không phải một phiếu xác nhận person độc
  lập.

Temporal hits chủ động chỉ giải quyết candidate chập chờn đã quan sát ở thùng rác.
Một thùng rác được detector nhận ổn định đủ ba hit vẫn có thể được confirmed; đó
là `I-05`/per-camera detector calibration ở patch kế tiếp, không được kéo thêm
logic vào `I-06` để che model false positive.

**Boundary của candidate chưa confirmed**

Candidate chưa confirmed bị chặn hoàn toàn khỏi:

- canonical/person overlay và dashboard overlay;
- public/live metadata;
- face recognition;
- smoking inference;
- event list, event lifecycle và evidence snapshot;
- Telegram/Zalo notification.

Candidate chỉ được tồn tại trong `_person_tracks` nội bộ và transition log/counter
chẩn đoán. Không có candidate overlay, candidate public metadata hoặc candidate
event riêng.

**Boundary sau confirmation**

- Confirmed track có detection thật ở frame hiện tại được dùng cho face/smoking
  analysis và canonical metadata theo policy camera hiện có.
- Confirmed track bị miss ngắn chỉ được giữ trong render view tối đa
  `render_grace_seconds: 1.0`; bbox cache không được dùng cho inference hay event
  update.
- Hết grace/lifecycle timeout, track biến mất theo owner hiện có; không tạo queue
  hoặc event bus mới.

## 2. Danh mục vấn đề duy nhất

Quy tắc đếm cố định:

- Có **10 hiện tượng** (`I-01` đến `I-10`).
- Mười hiện tượng thuộc **8 nhóm nguyên nhân** (`G-01` đến `G-08`).
- Có **3 nhóm đã xử lý** và **5 nhóm chưa xử lý**.
- Phiên kế tiếp chỉ thực hiện `I-06`, không có nghĩa toàn hệ thống chỉ còn một vấn
  đề.

| ID | Nhóm | Hiện tượng | Trạng thái | Phạm vi xử lý |
| --- | --- | --- | --- | --- |
| `I-01` | `G-01` Tracker ID | Dahua tạo ID person mới gần như mỗi frame do logic chuyển mép đối diện | **DONE** | Đã sửa trong HEAD và có regression test |
| `I-02` | `G-02` Mock readiness | Mock face/safety chết ở epoch 1 vì FFmpeg sống nhưng RTSP chưa đọc được video | **DONE** | Đã có `ffprobe` readiness và publisher monitoring |
| `I-03` | `G-03` Smoking overlay | Bbox Dahua nháy xanh/đỏ vì renderer dùng TTL bốn frame | **DONE** | Đã bỏ frame-based TTL |
| `I-04` | `G-03` Smoking overlay | TTL hai giây vẫn làm mất nhãn khi analysis trễ khoảng 3,8 giây | **DONE** | Smoking overlay đã theo temporal state của `track_id` |
| `I-05` | `G-04` Person false positive | Ghế Dahua bị detector nhận thành person và tạo sai smoking ROI | **OPEN** | Per-camera person calibration sau confirmation patch |
| `I-06` | `G-04` Person false positive | Thùng rác `camera_safety` bị nhận thành person chập chờn và bbox nháy | **OPEN — CURRENT SESSION** | Person confirmation gate + render grace tối thiểu |
| `I-07` | `G-05` Fire false positive | Tai/vật màu vàng bị nhận thành fire vì score vượt threshold global thấp | **OPEN** | Fire/smoke threshold và ROI riêng từng camera |
| `I-08` | `G-06` Analysis scheduling | Smoking và fire/smoke chạy tuần tự làm result age không đều | **OPEN** | Hai bounded latest-sample analysis lane |
| `I-09` | `G-07` Live latency | Live tích lũy trễ 30–40 giây nhưng dashboard vẫn báo `Live` | **OPEN** | HLS freshness watchdog và bounded output queue |
| `I-10` | `G-08` Process ownership | Foreground launcher kết thúc làm toàn runtime dừng | **OPEN** | Systemd service ownership |

Không được dùng số “4 vấn đề” cho tài liệu này. Số 4 chỉ từng xuất hiện do lấy ba
vấn đề cũ đã hoàn thành cộng với một mục tiêu phiên hiện tại, và không phản ánh
toàn bộ danh mục.

## 3. Nghiên cứu và bằng chứng

### 3.1 Tracker Dahua bị phá chuỗi temporal

Person detector vẫn trả một bbox ổn định, nhưng ID tăng liên tục. Smoking model vẫn
có score, tuy nhiên `smoking_score_histories` chứa rất nhiều ID và mỗi ID chỉ có
một mẫu. Điều này làm điều kiện hai hit trong cửa sổ bốn lần không thể hoàn tất.

Nguyên nhân nằm trong `opposite_frame_edge_transition()`:

- Bbox Dahua gần như chạm cả mép trên và mép dưới.
- Logic cũ đánh dấu chính bbox đó là một lần chuyển giữa hai mép đối diện.
- Matcher từ chối association, sau đó tạo `track_id` mới.

Sau khi sửa điều kiện “mép độc quyền”, Dahua giữ `track_id=1` qua chuỗi mẫu và
history tích đủ bốn score.

### 3.2 Startup mock RTSP không có readiness contract

Trong run cũ:

- Reader kết nối `face_mock`/`safety_mock` lúc 16:07:02.
- Publisher chỉ online trên MediaMTX lúc 16:07:33.
- Worker epoch 1 nhận lỗi source, thoát và được supervisor tạo lại ở epoch 2.

Sleep 4 giây chỉ chứng minh process FFmpeg chưa chết, không chứng minh MediaMTX đã
nhận được video stream. Readiness mới dùng `ffprobe` đọc được video trước khi
chuyển GStreamer pipeline sang PLAYING.

### 3.3 Nháy xanh/đỏ không phải smoking engine đổi quyết định

Màu hiển thị hiện tại:

- Xanh/cyan: person track tồn tại nhưng renderer không có smoking detection.
- Đỏ: cùng person track có smoking detection đã confirmed.

Smoking classifier chạy khoảng 400 ms/lần, trong khi Dahua phát nhanh hơn FPS mặc
định 5. Renderer cũ chỉ giữ cache bốn frame nên tự xóa nhãn trước inference kế
tiếp. Sau khi đổi sang TTL hai giây, runtime còn ghi nhận `result_age=3.856s` do
analysis worker chậm; nhãn vẫn có thể bị xóa dù engine đang confirmed.

Quyết định cuối:

- Smoking là state gắn với `track_id`, do `SmokingBehaviorEngine` sở hữu.
- Renderer giữ state đó đến khi engine thực sự clear hoặc track không còn trên
  frame hiện tại.
- Fire/smoke là camera-level detection nên vẫn dùng TTL hai giây để tránh giữ một
  environmental detection đã stale nếu analysis dừng.

### 3.4 Lần “không nhận diện hút thuốc” cuối

Trước restart, khi người thực sự có trong khung:

- `track_id=1` ổn định.
- Score smoking liên tiếp khoảng 60,5–66%.
- Track ở trạng thái `confirmed` và đã có smoking event.

Sau restart, chuỗi score chỉ khoảng 48–53%. Kiểm tra năm frame từ 16:42:13 đến
16:42:20 cho thấy:

- Ghế trống, không có người trong frame.
- Person detector nhận nhầm ghế gaming thành `person #1`.
- ROI smoking vì thế chứa ghế và phần lớn toàn cảnh.
- Không đủ bằng chứng hình ảnh để kết luận có hành vi hút thuốc.

Không hạ threshold smoking để “ép nhận diện”, vì hành động đó sẽ biến person false
positive thành smoking false positive và làm notification sai nghiêm trọng hơn.

### 3.5 Chất lượng ROI smoking

Khi người ngồi gần camera, person bbox có thể chiếm gần toàn chiều cao. Padding 20%
làm model ROI điển hình thành khoảng `[379, 0, 1920, 1080]`. Người vẫn nằm trong
ROI nhưng tín hiệu tay/miệng/điếu thuốc bị pha loãng bởi nền và bàn ghế. Đây là một
giới hạn chất lượng đầu vào của behavior classifier, không phải lý do để quay lại
cigarette object detector.

### 3.6 Fire false positive

Các frame đã quan sát gồm:

- Tai người bị nhận thành `FIRE` khoảng 32%.
- Vật trang trí màu vàng trên tường bị nhận thành `FIRE` khoảng 26–29%.

Threshold fire hiện là 25%, do đó các hard negative này vượt gate. Config ROI hiện
được khai báo ở cấp global trong khi comment mô tả riêng bối cảnh
`camera_safety`; cùng policy bị áp dụng cho Dahua là không đủ chặt chẽ.

### 3.7 Live buffer tăng dần và không có freshness contract

Đã quan sát dashboard phát chậm hơn thực tế khoảng 30–40 giây. Chưa có soak test
đủ dài để khẳng định độ trễ đã tăng tới nhiều ngày, nhưng kiến trúc hiện tại cũng
không có invariant hay watchdog để chứng minh điều đó không thể xảy ra.

Cấu hình hiện tại:

- MediaMTX tạo HLS MPEG-TS với segment 3 giây và giữ sáu segment.
- HLS.js dùng `liveSyncDurationCount: 3`, tức điểm phát mục tiêu danh nghĩa khoảng
  chín giây sau live edge.
- `liveMaxLatencyDurationCount: 8` tương ứng ngưỡng danh nghĩa khoảng 24 giây.
- `maxBufferLength: 15` chỉ điều khiển lượng media HLS.js cố tải trước; đây không
  phải trần độ trễ end-to-end.
- `backBufferLength: 0` chỉ dọn phần media đã phát phía sau playhead; nó không kéo
  playhead về live edge.

Dashboard hiện không ghi `hls.latency`, không so `video.currentTime` với
`hls.liveSyncPosition`, và không hard-seek/recreate player khi playback tiếp tục
phát nội dung cũ. Trạng thái `Live`/`3 of 3` chủ yếu phản ánh worker heartbeat và
khả năng mở stream, không chứng minh frame đang xem còn mới. Vì vậy một player
vẫn đọc được video cũ có thể bị coi là healthy.

Ở phía producer, queue trước output sink chưa khai báo rõ giới hạn nhỏ và
`leaky=downstream`. Đây chưa phải bằng chứng queue đó đã gây ra 30–40 giây, nhưng
là một điểm phải đo và khóa về latest-frame semantics để backpressure không tích
lũy frame cũ. Cần tách phép đo thành bốn lớp: source capture timestamp, pipeline
output timestamp, MediaMTX playlist edge và browser playback edge.

### 3.8 Thùng rác trên camera safety bị nhận thành person

Người dùng quan sát thùng rác bị bbox thành `person` liên tục nhưng ngắt quãng.
Đây gồm hai hiện tượng khác nhau:

1. False positive về semantics: detector nhầm thùng rác là người.
2. Flicker về lifecycle: candidate chỉ xuất hiện ở một số inference cycle, nên bbox
   được tạo rồi mất theo raw detection.

Không được sửa flicker bằng cách giữ mọi bbox lâu hơn, vì như vậy chỉ biến một
false positive ngắt quãng thành false positive ổn định. Pipeline hiện chưa lưu
dedicated trace gồm confidence/bbox/lifecycle cho toàn bộ raw person candidate;
do đó quan sát này chưa thể định lượng lại chỉ từ event evidence hiện có.

### 3.9 Bài học tối thiểu từ tracker Frigate

`frigate/` chỉ được dùng để đối chiếu thuật toán, không tham gia runtime. Phần cần
lấy chỉ là hai nguyên tắc: object mới phải có nhiều detection hit trước khi công
bố, và object đã công bố được giữ qua một khoảng miss ngắn. Motion/stationary,
Norfair/NvDCF, media và event architecture của Frigate không cần đưa vào patch
hiện tại.

Pipeline DeepStream đã có application tracker, `frames_seen`, `disappeared`,
velocity và bbox smoothing. Khoảng trống thực sự chỉ là `_assign_person_track_ids()`
công bố object ngay detection đầu tiên và consumer vẫn đọc raw object của từng
frame. Vì vậy giải pháp nhỏ nhất là thêm confirmation gate và time-based grace vào
chính tracker hiện tại, không tạo một tracker framework mới.

## 4. Thay đổi đã triển khai

### 4.1 Source

- `deepstream_safety/tracking.py`
  - Tách geometry helper để test độc lập.
  - Bbox chạm hai mép không còn bị coi là chuyển mép đối diện.
- `deepstream_safety/mock_input.py`
  - Chờ `ffprobe` đọc được RTSP video.
  - Fail rõ nếu publisher chết trước readiness.
- `deepstream_safety/pipeline.py`
  - Giám sát mock publisher trong toàn bộ vòng đời worker.
  - Dùng monotonic time cho tuổi analysis result.
  - Smoking overlay theo temporal state/track lifecycle, không theo renderer TTL.
  - Fire/smoke camera-level vẫn fail closed theo TTL.
  - Bổ sung `result_age_seconds` và `result_max_age_seconds` trong runtime status.
- `deepstream_safety/config.py`
  - Có kill switch `DEEPSTREAM_NOTIFICATIONS_ENABLED=false` cho acceptance an toàn;
    mặc định cấu hình thật vẫn bật notification.
- `deepstream_safety/config.yaml`
  - Khai báo `analysis_result_max_age_seconds: 2.0` cho environmental result.

### 4.2 Regression tests

- `tools/tests/unit/test_deepstream_tracking.py`
- `tools/tests/unit/test_deepstream_mock_input.py`
- `tools/tests/unit/test_safety_launcher.py`

Kết quả gần nhất:

- 15 targeted tests pass sau bản sửa ownership overlay.
- Ruff pass.
- Python compileall pass.
- `git diff --check` pass.

## 5. Runtime và artifact bàn giao

Các run quan trọng:

| Run ID | Mục đích | Kết quả chính |
| --- | --- | --- |
| `20260820T092003-ef825101` | Acceptance notification tắt | 3/3 live, epoch 1, Dahua giữ ID 1, browser không lỗi |
| `20260820T092612-65019ad2` | Cấu hình thật, notification bật | 3/3 camera ready, epoch 1 |
| `20260820T093352-93dd0e01` | Kiểm tra wall-clock TTL | Phát hiện analysis có lúc trễ 3,8 giây |
| `20260820T094013-60dbf6e3` | Bản cuối: smoking state không dùng renderer TTL | Chạy khoảng 1.693 giây (~28 phút) rồi cả ba worker dừng sạch cùng foreground launcher |

Artifact chẩn đoán hình ảnh:

- `.tmp/dahua-current-smoking-20260820T0942/`
  - Frame Dahua cho thấy ghế trống bị nhận nhầm thành person.
- `.tmp/overlay-stability-20260820T0936/`
  - Chuỗi frame kiểm tra output renderer trên camera safety.
- `.tmp/deepstream-safety/snapshots-acceptance-<run-id>/`
  - Manifest, event journal, SQLite index và evidence của từng run.

Không dùng launcher message, process existence hoặc HTTP 200 đơn lẻ làm acceptance.
Phải kiểm tra đồng thời epoch, heartbeat frame, analysis error, output RTSP/HLS và
event/evidence.

Tại thời điểm chốt tài liệu, runtime đang dừng. Ba worker dừng gần như đồng thời
lúc 17:10:55–17:10:58 sau khoảng 28 phút, log ghi shutdown sạch và không cho thấy
một worker crash riêng lẻ. Đây là lifecycle của foreground launcher, không được
ghi nhận nhầm thành inference/GStreamer instability.

## 6. Phương án triển khai đã chốt

### P0 — Đặt invariant độ trễ live end-to-end

Live production được phép buffer có chủ đích để ổn định, nhưng không được để độ
trễ tăng không giới hạn. Giữ HLS và bổ sung target, hard limit và recovery.

Patch đầu tiên chỉ làm ba việc:

1. Dashboard hiển thị `hls.latency` và drift tới `hls.liveSyncPosition`.
2. Drift vượt hard limit 15 giây thì seek về live sync position; playback không
   tiến sau một recovery attempt thì recreate HLS instance với cooldown.
3. Queue trước output sink đặt tối đa một đến hai buffer và latest-frame/leaky
   policy để không encode backlog cũ.

Acceptance: chạy đúng 30 giây và gây một lần stall/reconnect có kiểm soát;
latency phải quay về dưới hard limit mà không cần F5. Logic hard-limit/recovery
được kiểm tra bằng unit test với clock giả, không chờ độ trễ tích lũy ngoài đời.

Không tăng buffer và không đổi protocol.

### P0 — Person confirmation gate

Không tăng global person threshold ngay. Patch này chỉ ngăn raw detection chập
chờn được công bố và giữ bbox của person đã xác nhận qua miss ngắn.

#### Kiến trúc tối thiểu đã chốt

Giữ nguyên application tracker trong `pipeline.py`; không thêm tracker framework,
DTO hierarchy, motion tracker, stationary classifier hoặc NvDCF. Mỗi entry hiện có
trong `_person_tracks` chỉ cần thêm:

- `hit_times`: deque nhỏ chứa thời điểm detection match gần nhất.
- `confirmed`: object đã đủ confirmation gate hay chưa.
- `last_seen_at`: monotonic time của detection thật gần nhất.

Luồng duy nhất:

```text
raw person detection
  -> existing geometry association / bbox smoothing
  -> chưa đủ hit: giữ nội bộ, không đưa cho consumer
  -> đủ hit: confirmed person track
  -> miss ngắn: giữ bbox confirmed cho renderer
  -> hết grace: xóa track theo lifecycle hiện có
```

Không tạo synthetic NvDs detector object. `pipeline.py` cung cấp hai view từ cùng
`_person_tracks`:

- Current confirmed view: confirmed và có detection thật ở frame hiện tại; dùng
  cho `_person_rois()`, face recognition và smoking inference.
- Render confirmed view: confirmed và chưa quá grace; dùng cho live metadata/bbox.

Tentative detection không được vào hai view trên, event, evidence hay notification.

#### Patch triển khai cụ thể

1. Trong `_assign_person_track_ids()`, khi tạo track mới đặt `confirmed=False`,
   `hit_times=deque([now])`, `last_seen_at=now`; khi match thì append `now`, bỏ các
   hit cũ ngoài cửa sổ và set `confirmed=True` khi đủ hit.
2. Không tạo state enum. `confirmed` kết hợp `last_seen_at` và `disappeared` đã đủ
   biểu diễn tentative, active, grace và ended.
3. Thêm helper thuần trong `tracking.py` để trim/count time-window hits; helper này
   là phần duy nhất cần unit test độc lập.
4. Lọc raw NvDs object trong `_person_rois()` và `_recognition_tracks()` theo tập
   confirmed track ID của frame hiện tại.
5. Live metadata lấy bbox từ confirmed track cache trong grace window để detector
   miss ngắn không làm bbox nháy. Không chạy model trên bbox cache.
6. Khi track bị xóa theo timeout hiện có, để face/smoking/event owner nhận việc ID
   biến mất bằng contract hiện có; không tạo thêm event bus/lifecycle queue.
7. Chỉ log khi track chuyển `unconfirmed -> confirmed` hoặc bị xóa; status chỉ cần
   `person_tentative_count` và `person_confirmed_count`.

Cấu hình tối thiểu:

```yaml
person:
  tracking:
    confirmation_hits: 3
    confirmation_window_seconds: 1.5
    render_grace_seconds: 1.0
```

Tất cả cửa sổ dùng `time.monotonic()`. Không thêm score threshold thứ hai trong
patch lifecycle; `person.confidence` vẫn là detector gate hiện có. Sau patch
confirmation, patch kế tiếp cấu hình detector threshold riêng cho `camera_safety`
và `camera_dahua` bằng các clip thùng rác/ghế 30 giây. Không thay model.

Unit test bắt buộc chỉ gồm bốn case:

- Một hoặc hai hit chập chờn không được công bố.
- Ba hit trong cửa sổ công bố đúng một ID.
- Miss ngắn vẫn render nhưng không đi vào analysis.
- Hết grace/track timeout thì xóa và reacquire tạo lifecycle đúng.

Không benchmark NvDCF, không thêm person verifier, không thêm stationary/motion
logic và không xây telemetry framework trong patch này.

#### Gate trước khi đưa vào production

- Đoạn thùng rác/ghế chập chờn đã quan sát không tạo confirmed person, smoking
  event hoặc notification.
- Positive clips gồm người đứng yên, ngồi, đi vào/ra và che khuất ngắn vẫn tạo một
  confirmed track ổn định.
- Detector miss ngắn không làm bbox nháy; false candidate ngắt quãng không được
  render.
- Event lifecycle và evidence phải dùng verified track ID duy nhất; không phát
  `START/END` liên tục quanh một vật thể.
- Thùng rác được detector trả liên tục đủ confirmation gate được xử lý bằng
  per-camera detector threshold ở patch kế tiếp; không tăng state/timeout tracker.

### P0 — Tách và đo scheduling của các engine

Trong một camera worker, smoking và fire/smoke hiện chạy tuần tự trong một analysis
thread. Fire full-frame có thể kéo dài cadence của smoking.

Triển khai hai bounded latest-sample lanes trong cùng camera worker: một lane
person behavior và một lane environmental safety. Mỗi lane chỉ giữ sample mới
nhất, ghi inference latency/result age/drop count và không chạy trên GStreamer
thread.

Acceptance:

- Smoking p95 result age dưới 1 giây khi chạy đồng thời fire/smoke.
- Live output không giảm heartbeat và không restart worker.
- Event decision parity với baseline trên cùng video.

### P1 — Process ownership cho deployment production

`npm run camera:start` hiện là foreground launcher. Khi terminal/client kết thúc,
cleanup trap dừng dashboard, MediaMTX, mock publisher và mọi worker. Cơ chế này phù
hợp để debug nhưng không phải service lifecycle production.

Thêm một systemd unit sở hữu `multi_runner.py`; giữ launcher
foreground hiện tại cho debug. Windows launcher chỉ gọi start/stop/status của
unit. Không xây process orchestrator riêng.

Acceptance:

- Đóng terminal khởi động không làm runtime dừng.
- Stop command vẫn kết thúc sạch đúng các process Camera.
- Reboot WSL/Windows có startup policy xác định và audit được.
- Dashboard/status phân biệt `operator_stop`, `service_restart` và `worker_crash`.

### P1 — Nâng chất lượng smoking behavior ROI

Giữ kiến trúc person trace; không quay lại model cigarette detector làm quyết định
chính.

Chuyển smoking model sang upper-body crop suy ra từ confirmed person bbox. Không
thêm multi-crop/head/hand model.

Dataset cần có hard negatives: cầm bút, ngậm bút, cầm điện thoại gần miệng, uống
nước, chống cằm, tai/ánh sáng chói và ghế trống.

Acceptance:

- So sánh precision/recall/F1 và detection latency trên cùng acceptance set.
- Không đánh giá bằng một ảnh; phải dùng clip và event-level metric.
- Không hạ threshold chỉ để tăng recall nếu false notification tăng.

### P1 — Calibrate fire/smoke theo camera

1. Chuyển `class_rois` và threshold thành camera-profile override.
2. Thu hard-negative set từ chính Dahua: tai, bảng vàng, đèn, cửa sổ cháy sáng,
   vật màu đỏ/cam.
3. Calibrate threshold bằng precision-recall curve theo camera.
4. Giữ temporal confirmation hiện tại; không dùng hold để che model nhận sai.

Acceptance:

- Không còn event fire trên clip tai/bảng vàng chạy đúng 30 giây.
- Vẫn phát hiện các clip fire/smoke dương tính đã biết.
- Notification chỉ được mở sau event-level confirmation đạt gate.

### P2 — Observability và targeted acceptance

Bổ sung theo từng patch, không xây telemetry subsystem riêng:

- Person detector confidence và bbox source.
- Track age, missed frames và lý do END.
- Smoking raw score, confirmed state và số hit/clear hit.
- Inference latency/queue drop/result age theo function.
- Overlay source: current track hay cache.

Targeted gate, toàn bộ runtime acceptance chạy đúng 30 giây:

- Một cold start: mọi mock worker ở epoch 1 và có output frame.
- Live có một stall/reconnect cưỡng bức: tự phục hồi, không cần F5.
- Negative clip ghế/thùng rác/tai/vật vàng: không tạo event sai tương ứng.
- Positive smoking/fire/smoke/recognition clip: tạo đúng bbox và event mong đợi.
- Notification test một event với test recipient hoặc kill switch; sau đó
  khởi động lại cấu hình thật và xác nhận provider status.

Không dùng soak dài làm gate cho patch này. Các timeout dài được kiểm tra bằng
clock giả trong unit test; runtime acceptance chỉ xác nhận integration thật.

## 7. Kết quả audit chống over-engineering

Các rủi ro đã phát hiện và đã sửa trong tài liệu:

| Rủi ro thiết kế | Quyết định sau audit |
| --- | --- |
| Tạo `PersonTrackManager`, DTO/batch immutable và năm state trong khi tracker hiện tại đã có state cần thiết | Loại bỏ; mở rộng `_person_tracks` bằng ba field nhỏ |
| Thêm motion/stationary/NvDCF để xử lý một bbox chập chờn | Loại khỏi patch; không giải quyết semantic false positive |
| Thêm nhiều threshold/timeout chồng chéo | Chỉ giữ `confirmation_hits`, `confirmation_window_seconds`, `render_grace_seconds` |
| Dùng bbox cache cho face/smoking | Cấm; cache chỉ phục vụ renderer |
| Tracker cố che persistent model false positive | Cấm; chuyển thành patch calibration/model riêng |
| Xây telemetry/event bus mới | Loại bỏ; chỉ hai counter và transition log |
| Benchmark nhiều ROI/model/tracker cùng lúc | Hoãn; mỗi patch thay một biến và có acceptance riêng |
| Gộp live, tracker, model và service lifecycle vào một release | Cấm; triển khai thành các patch độc lập theo thứ tự dưới đây |

Rủi ro còn lại phải theo dõi khi triển khai confirmation gate:

- Người thật xuất hiện rất ngắn có thể chưa đủ ba hit; đây là trade-off chủ động
  để chặn detection một frame và phải được kiểm tra bằng clip positive.
- Render grace có thể hiển thị bbox cũ tối đa một giây; không được dùng bbox đó cho
  inference hay tạo event update.
- Persistent false positive vẫn có thể đủ ba hit. Không tăng confirmation window;
  patch kế tiếp chỉnh detector threshold per camera bằng hard-negative clip.
- Thay đổi filter consumer có thể vô tình làm mất face/smoking. Test phải chứng
  minh cùng confirmed ID đi xuyên suốt person → face/smoking → event.

## 8. Thứ tự triển khai khuyến nghị

1. Person confirmation gate tối thiểu và bốn unit test đã nêu.
2. HLS latency metric + hard seek/recreate + bounded output queue.
3. Calibration person threshold per-camera trên thùng rác safety và ghế Dahua;
   không sửa tracker lần hai.
4. Calibrate fire/smoke per camera để loại tai/vật vàng false positive.
5. Tách hai bounded latest-sample lane và ghi per-engine latency/result age.
6. Chuyển smoking inference sang upper-body ROI; không thêm multi-crop.
7. Thêm systemd service mode trước production deployment.
8. Chạy targeted runtime acceptance đúng 30 giây cho mỗi patch.

## 9. Cảnh báo bàn giao

- Các thay đổi source và test trong đợt này đang nằm trong working tree; không coi
  tài liệu này là bằng chứng đã commit/push.
- Runtime healthy không chứng minh model accuracy.
- `Live` không chứng minh frame đang xem còn mới; phải kiểm tra playback latency
  và timestamp của frame.
- Một bbox/event đúng không chứng minh precision/recall production.
- Không được sửa bằng cách ẩn bbox, lọc event khỏi danh sách hoặc hạ threshold để
  tạo cảm giác tính năng hoạt động.
- Telegram/Zalo chỉ nhận fire, smoke và smoking theo policy; danh sách event vẫn
  phải giữ đầy đủ các event chức năng. Zalo đã từng trả lỗi hết quota ngày, đây là
  external provider state chứ không phải inference failure.
- Không ghi token, chat ID hoặc credential vào log/tài liệu bàn giao.
