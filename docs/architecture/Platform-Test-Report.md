# Platform Runtime Test & Evidence Report

Tài liệu này là contract test dùng xuyên suốt Platform roadmap, tách khỏi tài liệu kiến trúc
[Platform.md](./Platform.md). Đây là test dùng chung cho toàn bộ runtime.

## 1. Mục đích và phạm vi

Chương trình test đọc trực tiếp MP4 gốc để dựng physical trace của `car_camera`, đồng thời chạy
runtime thực tế để thu các stage enrichment và media cho cả hai pipeline:

- `car_camera`: direct MP4 detection/tracking tạo physical trace; runtime bổ sung canonical Event,
  plate detection, OCR và Event publish.
- `face_camera`: detection, tracking, face candidate, embedding, identity và Event publish.

Đây là chương trình quan sát và lập báo cáo. Không có tiêu chí `pass/fail`, không có KPI
threshold để kết luận acceptance và không dùng `accepted=true/false` làm kết quả cuối.
Đây là entrypoint dùng để đánh giá runtime xuyên suốt roadmap; kết quả đánh giá là evidence
report và diagnostic data, không phải một cờ pass/fail.

## 2. Chương trình chạy test

Entrypoint chuẩn dùng xuyên suốt:

```powershell
# Local synchronous topology (default)
python tools/tests/e2e/run_platform_runtime_test.py

# External gRPC/mTLS topology; cùng implementation, chỉ khóa topology=external
python tools/tests/e2e/run_external_recognition_runtime_test.py
```

`tools/runtime/validate_platform_runtime.py` là implementation tham số hóa duy nhất. Wrapper
external không copy scorer, fixture, media writer hoặc report logic.

Mỗi invocation tự tạo một thư mục timestamp:

```text
.tmp/platform-runtime/YYYYMMDD-HHMMSS-mmm/
```

Script thực hiện:

1. Đọc trực tiếp MP4 LPR gốc, pre-calibrate motion state ngoài cửa sổ đo rồi chạy đúng
   motion-region, detector và tracker Frigate từ source time `0`; không qua RTSP, MediaMTX,
   transcode, black frame hoặc freeze.
2. Ghi `direct-lpr-tracks.json`; raw tracker ID chỉ là lineage, physical trace được ngắt online
   khi cùng raw ID có chuyển động đảo chiều phi vật lý. Fixture và OCR không tham gia tách trace.
3. Chuẩn bị runtime media và danh sách plate audit dùng riêng cho bước đối chiếu cuối.
4. Khởi động topology đã chọn. Local chỉ có Frigate; external khởi động thêm
   `camera-recognition`, chờ standard health và xác nhận Frigate không load local Face/LPR model.
5. Mở capture gate bằng `run_id`, sau đó trigger đồng thời mỗi publisher phát video nguồn
   đúng một lần rồi tự trở về standby.
6. Ghi source PTS/anchor và thu passage trace, runtime LPR evidence, hardware/runtime samples
   chỉ trong cửa sổ capture của run hiện tại.
7. Chờ deferred recognition/evidence lifecycle và native recording segment được commit.
8. Lấy `Event.start_time/end_time` và tải clip bằng recording API có sẵn của Frigate.
9. Thu Docker inspect/log trước khi restore runtime.
10. Xuất `report.md`, JSON report, direct trace, runtime evidence và clip theo trace.

Hai entrypoint trên đều gọi `tools/runtime/validate_platform_runtime.py`; không có hai runtime
test implementation.

## 3. Trace bắt buộc

### 3.1 Car/LPR pipeline

Mỗi record phải giữ source PTS, camera, track ID, generation, candidate ID, frame reference,
bbox, evidence ID, quality score/components, decision, reason và runtime timestamp nếu có.

Trace LPR phải báo từng bước, kể cả bước bị thiếu hoặc bị reject:

- `detector_hit`
- `track_seen` hoặc `track_candidate`
- `lpr_eligible`
- `plate_detector_input`
- `plate_detector_result`
- `plate_crop`
- `ocr_plate_input`
- `ocr_result`
- `ocr_text_crop`
- `ocr_recognition_tensor`
- `event_published`

### 3.2 Face pipeline

Trace Face phải tách được:

- `detector_hit`
- `first_qualified_face`
- `candidate_submitted`
- `first_attempt`
- `confirmed_result`

Kết quả phải giữ person bbox, raw detector face bbox, effective crop bbox,
candidate/frame/evidence lineage, raw identity/score, aggregate identity/score và terminal reason.

## 4. Quy tắc trace và đối chiếu kết quả

- Pipeline tự tạo trace từ detector/tracker và recognition lifecycle; fixture không được tạo,
  tách, hợp nhất hoặc đổi trace.
- `direct-lpr-tracks.json` là bằng chứng authoritative cho số physical trace LPR. Runtime raw
  tracker ID chỉ là lineage và có thể được nhiều physical trace kế tiếp sử dụng.
- LPR chỉ đối chiếu final `event_published.plate` của mỗi trace với danh sách `expected_plate`
  duy nhất sau khi pipeline hoàn tất. Không dùng fixture time/bbox/ROI để gán xe.
- PTS, bbox và candidate lineage vẫn phải ghi đầy đủ để kiểm tra pixel/result/evidence cùng frame.
- Face report đếm trực tiếp producer-owned raw `trace_id`; kết quả `unknown` là recognition outcome
  hợp lệ và không được biến thành failure hoặc dùng fixture để đặt tên track.
- Mỗi invocation chạy đúng một replay round; nếu cần so sánh nhiều lần thì mỗi lần có một
  thư mục timestamp riêng.
- Mọi trace/evidence phải mang đúng `run_id` của invocation. Runtime không ghi warm-up
  trước capture gate và report không đọc record của run cũ.
- Report chỉ có một bảng runtime lineage LPR, mỗi producer trace đúng một hàng. Bảng dùng các cột
  ngắn `Clip`, `Outcome`, `Track`, `Eligible`, `Plate`, `OCR`, `Publish`; trạng thái, output và
  reason cuối nằm ngay trong stage cell. Fixture/expected plate chỉ tham gia KPI tổng hợp, không
  tạo hàng hoặc cột trong bảng runtime.
- Report chỉ có một bảng lineage Face từ raw producer `trace_id`; mỗi hàng dùng ba stage nghiệp vụ
  production: `Prepare face`, `Recognition`, `Decision / publish`. Guard/cadence, evidence resolve,
  input-frame count và evidence writer là boundary/side effect để truy vết, không phải Face stage.
  Trace labels chỉ kiểm chứng count/result. Bbox/crop chi tiết thuộc lifecycle ngay dưới bảng.
- `measurement_valid` chỉ mô tả tính đầy đủ/hợp lệ của dữ liệu đo, không phải acceptance.
- Anchor/fixture video không được dùng để dựng lại clip. Media của trace chỉ được lấy từ
  canonical Event và recording API của runtime đang được đo.

## 5. Metrics chức năng và pipeline

### Car/LPR

- Detection hit count và track coverage.
- Runtime trace count và final plate-only comparison.
- LPR eligibility count/reason.
- Plate detector count, score, bbox và latency.
- OCR invocation count, text box count, raw text, character scores và latency.
- Event publish count, plate, score, candidate/frame/evidence lineage.
- Missing stage, duplicate candidate, plate mismatch và output không thuộc danh sách audit.

### Face

- Detection/qualified candidate count.
- Candidate submission và inference attempts.
- Raw identity/score, weighted-vote aggregate và publication reason.
- Confirmed/unknown/ambiguous result.
- Candidate/frame/evidence lineage.
- Passage-to-confirmed, eligible-to-confirmed, first-attempt và embedding latency.

## 6. Metrics hiệu năng phần cứng và runtime

Report phải ghi sample theo thời gian và giá trị cực đại/tối thiểu khi phù hợp:

- Container RAM.
- CPU usage của Frigate/process/container.
- GPU utilization.
- GPU memory/VRAM.
- `/dev/shm` usage.
- Camera/detector skipped FPS và source PTS gap.
- Capture/detected-object/recognition/LPR queue depth và queue age.
- Evidence bytes/pinned ownership.
- Recognition active lifecycle, in-flight, selector depth và terminal cleanup.
- Detector, plate detector, OCR và face inference calls/s và latency P50/P95.
- Model load count.
- Runtime duration, replay duration và restore duration.
- Restart, reconnect, FFmpeg stall, traceback, thread exit và DB I/O error.

Metrics hardware/runtime chỉ là quan sát chẩn đoán; không chuyển thành pass/fail.

## 7. Artifact đầu ra

Mỗi lần chạy phải lưu tối thiểu:

| Artifact | Nội dung |
| --- | --- |
| `report.md` | Run header; khối LPR và Face tách riêng; hardware/runtime health rõ đơn vị; link gallery evidence thật |
| `summary.json` | Report tổng hợp, measurement, runtime và diagnostic data |
| `runtime-trace.json` | Trace detector/track/Event/Face/LPR theo source PTS |
| `runtime-evidence.json` | LPR invocation, crop, plate detector và OCR evidence |
| `native-media.json` | Quan hệ `trace_id → Event.id → recording segments → clip.mp4`, hash và ffprobe |
| `face.json` | Bảng Face passage × round và latency |
| `lpr.json` | Bảng car passage × round, funnel và raw plate result |
| `container-inspect.json` | Docker container configuration/state |
| `container.log` | Log runtime trong cửa sổ test |
| `container-inspect-recognition.json` | External service state; chỉ bắt buộc ở external topology |
| `container-recognition.log` | External service log; chỉ bắt buộc ở external topology |
| `external-recognition-evidence.json` | Hash/shape/bbox/stage audit của producer-owned artifacts |
| `media/images.md` | Gallery đầy đủ theo producer trace/evidence; gồm mọi LPR artifact và bộ Face raw attempt/bbox/crop |

Media được giữ theo cấu trúc:

```text
media/
├── runtime-trace.jsonl          # raw trace của đúng run_id hiện tại
├── lpr/<safe_trace_id>/
│   ├── clip.mp4                # tải từ native recording API
│   ├── trace.json
│   └── <evidence_id>/*.jpg
└── face/<safe_trace_id>/
    ├── clip.mp4
    ├── trace.json
    └── <evidence_id>/
        ├── evidence.json
        ├── *-recognition_attempt.jpg
        ├── *-recognition_attempt_bbox.jpg
        └── *-face_crop.jpg
```

`recognition_attempt_bbox` do shared Face producer tạo trong chính model attempt. Validator kiểm
tra hash, byte size, image shape, `object_box`, raw `detail_box` và `effective_crop_box`; validator
không vẽ bbox thay thế và không tạo evidence ID/track ID giả.

`report.md` chỉ giữ link và tổng số ảnh để không kéo dài bảng kết quả. `media/images.md` hiển thị
toàn bộ ảnh trong các nhóm có thể thu gọn theo trace. Mỗi ảnh nằm trong timeline có sequence,
source PTS, stage, evidence ID, decision/result, bbox/crop, byte size và SHA-256 để truy ngược
failure. LPR lấy record từ `runtime-evidence.json`; Face lấy record từ producer
`media/face/evidence.jsonl`, không suy diễn metadata từ tên file.

Kho `recordings`, SQLite, snapshot/cache và enrollment phục vụ Frigate nằm trong workspace
runtime tạm, không nằm trong cây report. Script đọc recording khi Frigate còn chạy và tải clip
thẳng vào trace tương ứng; report không sinh rồi xóa hoặc di chuyển các thư mục media trung gian.

Detector observation không phải lifecycle trace, không có thư mục hoặc clip riêng. Passage
không tạo được runtime trace được báo `trace_id = -`; script không tạo trace/video giả,
không fallback sang cắt fixture và cũng không cắt recording theo timestamp khi chưa resolve
được đúng một canonical Event đã kết thúc.

Report phải ghi SHA-256 và byte size của artifact quan trọng. Artifact thiếu hoặc hash sai được
ghi là report `incomplete`; đây là kiểm tra tính đầy đủ của bằng chứng, không phải đánh giá chất
lượng nhận dạng.

`summary.json` có hai lớp metric trace: `runtime.trace_metrics` cho detector/tracker/Event/Face,
và `runtime.lpr_evidence_trace_metrics` cho eligibility, plate detector, crop và OCR. Cả hai
đều ghi `stage_counts` và `stage_calls_per_second`; dữ liệu chi tiết vẫn giữ nguyên trong raw
trace/evidence để đối chiếu từng record.

`report.md` cố ý không lặp raw JSON, throughput, provenance/hash hoặc diagnostic dump. Chi tiết
đầy đủ vẫn thuộc các JSON authoritative. Markdown chỉ giữ một hàng canonical cho mỗi lineage;
LPR và Face không dùng chung bảng kết quả.

Riêng lineage LPR có kết quả `UNEXPECTED` hoặc `NO_OUTPUT` phải có `Lifecycle traces` ngay dưới
khối LPR. Mỗi stage ghi số record, source PTS, status, final decision/result và render trực tiếp
ảnh producer tương ứng; stage thiếu phải hiện `MISSING`, không được dựng ảnh thay thế.
Face có outcome `recognized_unknown` hoặc `not_recognized` phải có lifecycle ngay dưới khối Face.
`recognized_unknown` giữ nguyên nhãn terminal hợp lệ nhưng vẫn được review nhận dạng; không được
  báo sai thành transport/pipeline failure. Mỗi attempt phải là đúng một hàng theo ba stage nghiệp
  vụ source: `prepare_face_attempt → recognizer.classify → FaceEngine weighted vote / publish`.
  `process_frame` guard, adapter/core cadence, evidence resolve, Event adapter và evidence writer
  chỉ là boundary/side effect quanh pipeline. Các passage trace labels chỉ là observability
  evidence, không phải tên stage production. Lifecycle Face dùng cùng kết cấu với LPR:
  `Stage`, `Records`, `Source PTS`, `Status`, `Final result`, `Image`; mỗi production stage chỉ
  render tối đa một ảnh producer. Các artifact còn lại vẫn nằm trong gallery để truy vết, không
  được nhân thành hàng hoặc ảnh lifecycle riêng.
Thumbnail dùng cùng chiều rộng `240px`, giữ nguyên tỷ lệ và lazy-load để không kéo vỡ bảng;
click thumbnail phải mở file producer gốc ở độ phân giải đầy đủ.

## 8. Tổng hợp nhiều run

Script tổng hợp:

```powershell
python tools/reporting/summarize_platform_runtime.py `
  .tmp/platform-runtime-1/summary.json `
  .tmp/platform-runtime-2/summary.json `
  .tmp/platform-runtime-3/summary.json `
  --output .tmp/platform-runtime-evidence-report.json
```

Script tổng hợp nhận ba summary của ba thư mục timestamp khi cần so sánh nhiều lần, giữ chi tiết
từng run, passage/round, runtime evidence, source hash, diagnostic values và worst-run metrics.
Kết quả chuẩn có dạng:

```json
{
  "mode": "evidence_only",
  "acceptance": {
    "status": "not_scored",
    "criteria": []
  },
  "report_complete": true
}
```

## 9. Test code/regression

Các regression test kiểm tra scorer, Face passage assignment, đối chiếu final plate LPR,
OCR/LPR result và lifecycle:

```powershell
python -m pytest tools/tests/unit -q
```

Regression test chỉ xác nhận tính đúng của chương trình đo và schema/report; không thay thế
runtime replay và không được dùng để tuyên bố hệ thống nhận dạng đạt chất lượng production.

Execution gate và yêu cầu dùng `deploy/run.ps1` được quản lý tập trung tại
[AGENTS.md](../../AGENTS.md); report này chỉ ghi evidence của các bước đã chạy.

## 10. Bằng chứng hiện có

Các report runtime gần nhất dùng để review topology:

| Run | Topology | Runtime evidence |
| --- | --- | --- |
| [`20260812-141524-728`](../../.tmp/platform-runtime/20260812-141524-728/report.md) | external | report complete; `measurement_valid=true`, Face `4/4` coverage `1.0`, `38` track-seen, raw LPR `11/11`, LPR exact `7/11` diagnostic, API/SQLite consistency, correlation mismatch `0`, không reconnect/stall, service healthy, local model `0`, cleanup/pending/writer `0`, runtime restored |
| [`20260812-130708-272`](../../.tmp/platform-runtime/20260812-130708-272/report.md) | external fault: stream_disconnect | artifact completed; network disconnect/reconnect recorded, topology restored |
| [`20260812-132857-884`](../../.tmp/platform-runtime/20260812-132857-884/report.md) | external fault: client_disconnect | artifact complete; typed lifecycle failure, publication safety, cleanup zero and topology restore recorded |
| [`20260812-135955-917`](../../.tmp/platform-runtime/20260812-135955-917/report.md) | external fault: service_restart | artifact complete; service epoch interruption, typed lifecycle failure, publication safety, cleanup zero and topology restore recorded |
| [`20260812-013537-853`](../../.tmp/platform-runtime/20260812-013537-853/report.md) | external | report complete; 4/4 Face lineage (`3 known + 1 unknown`), 11 raw LPR lineage, 20 producer bbox bundles, deadline/failure `0`, service healthy, local model load `0`, cleanup/pending/writer `0` |
| [`20260812-013921-643`](../../.tmp/platform-runtime/20260812-013921-643/report.md) | local | report complete; 4/4 Face lineage (`3 known + 1 unknown`), 11 raw LPR lineage, deadline/failure `0`, cleanup/pending/writer `0` |

Đây là các invocation độc lập nên số attempt và raw LPR terminal output có thể khác theo tracker/
wall-clock scheduling. Bit-exact decision parity chỉ được kết luận từ differential test khi đưa
cùng ordered observations vào các topology. Run healthy mới đã có correlation/measurement/API/SQLite/
reconnect-stall hợp lệ; ba fault scenarios và packaging đã có artifact pass. LPR accuracy vẫn là
diagnostic, vì vậy `report complete` không được diễn giải thành kết luận accuracy production.

## 11. Phase 8 tracker edge — source gate ngày 2026-08-12

Phase 8 hiện mới có bằng chứng source/unit cô lập, chưa có build/runtime acceptance:

- `frigate/tests/test_tracker_edge.py`: `18 passed` cho proto compatibility, mTLS identity,
  ownership/mixed mode, producer parity, evidence TTL/checksum/pin, durable journal replay/ACK/
  restart/spool-full, canonical SQLite ingest, media manifest/range và no direct recognition import.
- Root unit gate: `77 passed`; nhóm tracker/config/camera-maintainer/stationary/Event canonical:
  `96 passed`; `compileall`, Ruff, PowerShell parser và `git diff --check` pass.
- Full Frigate gate collect đủ `976` test nhưng dừng tại
  `TestHttpApp.test_recordings_storage_requires_admin`: fixture dùng `os.path.join` trên Windows tạo
  key `/media/frigate\\recordings`, khác runtime constant POSIX `/media/frigate/recordings`. Đây là
  hard-gate failure ngoài Phase 8; không chỉnh fixture/report để biến thành pass.
- Theo execution gate, chưa chạy build `camera-tracker`, healthy E2E, fault E2E hoặc restore.
  Không được diễn giải source/unit pass thành Phase 8 `[DONE]`.
