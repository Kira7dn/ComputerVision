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
python tools/tests/e2e/run_platform_runtime_test.py
```

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
4. Khởi động Frigate, chờ camera, detector, model và image readiness.
5. Mở capture gate bằng `run_id`, sau đó trigger đồng thời mỗi publisher phát video nguồn
   đúng một lần rồi tự trở về standby.
6. Ghi source PTS/anchor và thu passage trace, runtime LPR evidence, hardware/runtime samples
   chỉ trong cửa sổ capture của run hiện tại.
7. Chờ deferred recognition/evidence lifecycle và native recording segment được commit.
8. Lấy `Event.start_time/end_time` và tải clip bằng recording API có sẵn của Frigate.
9. Thu Docker inspect/log trước khi restore runtime.
10. Xuất `report.md`, JSON report, direct trace, runtime evidence và clip theo trace.

`tools/runtime/validate_platform_runtime.py` là implementation; entrypoint chuẩn duy nhất là
`tools/tests/e2e/run_platform_runtime_test.py`.

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

Kết quả phải giữ person bbox, face bbox, candidate/frame/evidence lineage, raw top-1/top-2
outcome, margin, identity và terminal reason.

## 4. Quy tắc trace và đối chiếu kết quả

- Pipeline tự tạo trace từ detector/tracker và passage registry online; fixture không được tạo,
  tách, hợp nhất hoặc đổi trace.
- `direct-lpr-tracks.json` là bằng chứng authoritative cho số physical trace LPR. Runtime raw
  tracker ID chỉ là lineage và có thể được nhiều physical trace kế tiếp sử dụng.
- LPR chỉ đối chiếu final `event_published.plate` của mỗi trace với danh sách `expected_plate`
  duy nhất sau khi pipeline hoàn tất. Không dùng fixture time/bbox/ROI để gán xe.
- PTS, bbox và candidate lineage vẫn phải ghi đầy đủ để kiểm tra pixel/result/evidence cùng frame.
- Face giữ passage association riêng vì kết quả `unknown` không có chuỗi identity để match.
- Mỗi invocation chạy đúng một replay round; nếu cần so sánh nhiều lần thì mỗi lần có một
  thư mục timestamp riêng.
- Mọi trace/evidence phải mang đúng `run_id` của invocation. Runtime không ghi warm-up
  trước capture gate và report không đọc record của run cũ.
- Bảng LPR lấy runtime trace làm hàng; bảng fixture comparison chỉ bổ sung expected plate nếu
  final plate match duy nhất. Face tiếp tục dùng passage × round; một invocation có `round_id=1`.
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
- Top-1/top-2 raw result, margin và identity.
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
| `report.md` | Metrics/kết quả tổng hợp và failure trace theo từng `track_id`, lifecycle step, link image thật nếu có |
| `summary.json` | Report tổng hợp, measurement, runtime và diagnostic data |
| `runtime-trace.json` | Trace detector/track/Event/Face/LPR theo source PTS |
| `runtime-evidence.json` | LPR invocation, crop, plate detector và OCR evidence |
| `native-media.json` | Quan hệ `trace_id → Event.id → recording segments → clip.mp4`, hash và ffprobe |
| `face.json` | Bảng Face passage × round và latency |
| `lpr.json` | Bảng car passage × round, funnel và raw plate result |
| `container-inspect.json` | Docker container configuration/state |
| `container.log` | Log runtime trong cửa sổ test |

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
    └── <evidence_id>/*.jpg
```

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

## 10. Bằng chứng hiện có

Report evidence gần nhất nằm tại thư mục timestamp dưới `.tmp/platform-runtime/`. Mỗi thư mục
là một invocation độc lập, chứa một lần replay nguồn LPR liên tục có 11 biển số audit và replay
Face tương ứng. Số trace LPR là kết quả thực tế do detector/tracker tạo ra, không mặc định bằng
11. Các kết quả nhận dạng, runtime và hardware phải đọc từ artifact raw tương ứng, không suy
diễn từ fixture hoặc một cờ acceptance.
