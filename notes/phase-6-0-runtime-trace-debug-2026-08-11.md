# Phase 6-0 — Runtime trace và native media

Ngày ghi nhận: 11/08/2026

Trạng thái Phase 6-0: `[DONE]`. Trạng thái này xác nhận runtime test, source ordering, raw trace và
native media đã hoàn thành; không có nghĩa recognition quality đã đạt.

## 1. Vấn đề

Runtime test từng cho kết quả không phản ánh đúng pipeline Frigate:

- Một video có 11 lượt xe nhưng số lượng/thành phần trace thay đổi bất thường.
- Nhiều xe hoặc frame không liên quan xuất hiện trong cùng một trace; có đoạn frame đen.
- Có trace sinh thêm suffix `p2`, `p3`, làm không rõ đâu là tracker ID thật.
- Clip bị thiếu hoặc nằm ngoài thư mục trace; một số clip được tìm và gom lại sau khi pipeline đã
  kết thúc.
- Test từng tạo các thư mục trung gian như `recordings`, `exports`, `staging`, `replay` rồi cleanup.
- Measurement có thể kết thúc trước khi finite MP4 được xử lý hết, làm mất trace hoặc clip cuối.
- Fixture time/bbox và logic passage riêng từng tham gia gán trace, trong khi fixture chỉ nên dùng
  để so sánh kết quả cuối cùng.

Hệ quả là report có thể trông đầy đủ nhưng không chứng minh được một trace thực sự thuộc một xe và
media được tạo từ đúng lifecycle của pipeline.

## 2. Nguyên nhân

### 2.1 Kiến trúc test đứng ngoài pipeline

Test từng dựng thêm identity/passage và regroup media sau khi có kết quả. Những ID suy diễn như
`p2`, `p3` không phải raw Frigate tracker ID. Cách này che mất lifecycle thật và có thể ghép asset
của nhiều object vào cùng một thư mục.

### 2.2 Finite MP4 dùng nhầm chính sách của live stream

Capture queue từng áp dụng latest-only replacement cho cả nguồn file. Khi detector chậm, frame cũ
bị bỏ và frame mới thay vào. Với live stream đây là cách giới hạn latency, nhưng với finite MP4 nó
làm mất chuỗi frame cần cho tracker, tạo source gap và có thể retire hoặc nối track sai.

### 2.3 Timestamp phụ thuộc tốc độ chạy decoder

Timestamp wall-clock không đại diện ổn định cho vị trí frame trong MP4 khi pipeline bị
backpressure. Detect/Event/recording có thể lệch timeline, làm cửa sổ clip bị âm hoặc không phủ đúng
lifecycle của object.

### 2.4 Chốt phép đo trước khi pipeline drain hết nguồn

Validator từng suy ra thời điểm kết thúc từ duration thay vì nhận tín hiệu producer đã enqueue frame
cuối và consumer đã xử lý tới frame đó. Vì vậy trace/recording cuối nguồn có thể chưa hoàn thiện khi
report bắt đầu thu kết quả.

## 3. Hành động khắc phục

1. Đưa MP4 gốc trực tiếp vào capture/detect/track Frigate; không dựng tracker hoặc passage registry
   trong test.
2. Tách chính sách queue theo loại nguồn:
   - finite local MP4: FIFO/backpressure, không drop source frame;
   - live/network stream: latest-only để giữ latency bounded.
3. Gán timeline ổn định theo source index:

   ```text
   frame_time = source_epoch + frame_number / detect_fps
   ```

4. Producer ghi `{camera}.start` khi có frame đầu và `{camera}.end` sau khi enqueue frame cuối.
5. Validator chỉ chốt `capture_cutoff` sau khi:
   - nhận đủ EOF marker của mọi camera;
   - `latest.jpg`/processed timestamp của từng camera đã đi qua timestamp cuối nguồn.
6. Dùng raw Frigate tracked-object ID làm `trace_id` duy nhất. Không sinh alias, passage ID hoặc
   suffix `p2/p3` để sửa kết quả hậu kỳ.
7. Fixture LPR chỉ giữ `lpr-01…lpr-11` và `expected_plate`; không giữ time/bbox/ROI để tạo hoặc gán
   runtime trace.
8. Dùng clip do recording/export lifecycle của Frigate tạo. Mỗi trace lưu:

   ```text
   media/<pipeline>/<trace_id>/
   ├── clip.mp4
   ├── trace.json
   └── <evidence_id>/...
   ```

   `<evidence_id>` là một candidate/lần xử lý trong trace, không phải track mới.
9. Mỗi invocation chạy đúng một vòng và lưu vào một thư mục timestamp riêng. Không tạo asset ngoài
   luồng rồi di chuyển hoặc cleanup sau.
10. Cập nhật `Platform.md` để Phase 6-0 mô tả contract runtime hiện hành. Quyết định sau đó đã
    deferred rolling top-3/best-valid-result và chọn Phase 6-1 restore Frigate master semantics.

## 4. Kết quả

Run kiểm chứng hiện tại:

- Artifact: `.tmp/platform-runtime/20260811-044205-810/`
- Report: `.tmp/platform-runtime/20260811-044205-810/report.md`
- Trạng thái report: `complete`
- Measurement valid: `true`
- Tổng thời gian: `129,302 s`
- Replay: `39,195 s`
- Restore: `13,961 s`
- Runtime restored: `true`
- Restart delta: `0`
- `car_camera skipped_fps`: `0,0`
- Raw LPR trace folders: `11`
- Native LPR clips: `11/11`
- Tổng native clips gồm Face: `12/12`
- Capture queue/timeline regression: `3 passed`
- Passage acceptance unit test: `44 passed`

Kết quả nhận dạng trong report hiện tại:

| Hạng mục | Kết quả |
| --- | ---: |
| LPR exact match | `7/11` (`0,636`) |
| LPR precision | `0,875` |
| Face accuracy/recall | `0,0 / 0,0` |
| Evidence pinned zero | `false` |

## 5. Tồn đọng

Phase 6-0 đã xác minh được source ordering, raw trace ownership và native media, nhưng chưa xác nhận
recognition hoàn thiện:

- Visual lineage audit cho thấy trace `...-61ybnw` theo xe biển `3789`, nhưng một invocation muộn
  tiếp tục dùng predicted bbox đã stale và OCR ra `3B53567` sau khi xe gốc rời khung. Trace
  `...-8cixfr` mới là xe `3B53567` thực tế.
- Đây là lỗi downstream LPR admission/representative timing, không phải lý do đổi raw tracker ID
  hoặc ghép/tách trace hậu kỳ.
- Face chưa tạo kết quả nhận dạng đúng trong run này.
- Evidence lease chưa drain hoàn toàn (`evidence_pinned_zero=false`).

## 6. Phase tiếp theo

Phase 6-1 trả recognition voting/consensus về Frigate `master` commit
`50a2b6729eb152d9512b100c78c55fa84dffa430` trước khi tiếp tục sửa detection boundary hoặc stale
bbox. LPR phải dùng exact master variant clustering/representative; Face phải dùng exact master
weighted voting, active `min_faces`, count-tie rejection và attempt limits. Custom
`BestResultReducer`/rolling top-3 không còn là production target.

Sau khi differential test khóa master parity, stale predicted bbox mới được xử lý ở phase kế tiếp
bằng provenance/admission guard mà không sửa voting semantics. Không sửa fixture, không tạo ID phụ
và không regroup media sau pipeline.
