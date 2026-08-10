# Platform Runtime Test & Evidence Report

Tài liệu này là contract test dùng xuyên suốt Platform roadmap, tách khỏi tài liệu kiến trúc
[Platform.md](./Platform.md). Đây là test dùng chung cho toàn bộ runtime.

## 1. Mục đích và phạm vi

Chương trình test chạy replay thực tế qua Frigate cho cả hai pipeline:

- `car_camera`: detection, tracking, canonical Event, plate detection, OCR và Event publish.
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

1. Chuẩn bị fixture/replay và manifest ground truth.
2. Khởi động Frigate và hai replay camera.
3. Chờ camera, detector, model và image readiness.
4. Ghi source PTS/anchor của từng round.
5. Thu passage trace, runtime LPR evidence và hardware/runtime samples.
6. Chờ deferred recognition/evidence lifecycle kết thúc để ghi trạng thái cuối.
7. Thu Docker inspect/log trước khi restore runtime.
8. Xuất `report.md`, JSON report, raw trace và runtime evidence.
9. Gom replay nguồn vào `media/replays/face` và `media/replays/lpr`; giữ report, summary, trace/evidence, log, fixture/config và DB passage.

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

## 4. Quy tắc đo và gán passage

- Dùng source PTS của video, không dùng wall-clock để suy thời gian nguồn.
- Mỗi record được gán one-to-one vào một physical passage và một replay round.
- Track chạm nhiều passage phải được báo là `track_switch`; không gán toàn bộ trajectory vào
  passage có IoU lớn nhất.
- Record thiếu PTS, bbox hoặc candidate lineage được ghi `unscorable`/`lineage_missing`.
- Mỗi invocation chạy đúng một replay round; nếu cần so sánh nhiều lần thì mỗi lần có một
  thư mục timestamp riêng.
- Bảng báo cáo tối thiểu là passage × round cho cả car và face; ở một invocation `round_id=1`.
- `measurement_valid` chỉ mô tả tính đầy đủ/hợp lệ của dữ liệu đo, không phải acceptance.

## 5. Metrics chức năng và pipeline

### Car/LPR

- Detection hit count và track coverage.
- Passage/round assignment.
- LPR eligibility count/reason.
- Plate detector count, score, bbox và latency.
- OCR invocation count, text box count, raw text, character scores và latency.
- Event publish count, plate, score, candidate/frame/evidence lineage.
- Missing stage, wrong assignment, duplicate candidate và false passage.

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
| `face.json` | Bảng Face passage × round và latency |
| `lpr.json` | Bảng car passage × round, funnel và raw plate result |
| `container-inspect.json` | Docker container configuration/state |
| `container.log` | Log runtime trong cửa sổ test |

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

Các regression test kiểm tra scorer, assignment, physical passage, OCR/LPR result và lifecycle:

```powershell
python -m pytest tools/tests/unit -q
```

Regression test chỉ xác nhận tính đúng của chương trình đo và schema/report; không thay thế
runtime replay và không được dùng để tuyên bố hệ thống nhận dạng đạt chất lượng production.

## 10. Bằng chứng hiện có

Report evidence gần nhất nằm tại thư mục timestamp dưới `.tmp/platform-runtime/`. Mỗi thư mục
là một invocation độc lập, chứa một round và 11 passage LPR cùng Face passage tương ứng. Các kết
quả nhận dạng, runtime và hardware phải đọc từ artifact raw tương ứng, không suy diễn từ một cờ
acceptance.
