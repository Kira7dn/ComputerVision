# LPR upstream parity và replay ngày 09/08/2026

## Kết luận

Production path LPR hiện chạy logic realtime của Frigate upstream, tích hợp vào runtime hiện có bằng
adapter trong `EmbeddingsMaintainer`. Audit ngày 10/08/2026 xác nhận validator hiện tại **không đủ
điều kiện công bố KPI độ chính xác**. Các số `4/11`, passage recall và recognition precision/recall
bên dưới là output lịch sử của scorer cũ, chỉ giữ lại để truy vết artifact; chúng không còn được xem
là phép đo chính xác theo physical passage.

Run mới nhất dùng nguồn 1820×1024/5 FPS nằm tại
[`summary.json`](../../.tmp/platform-lpr-1024p-5fps-final/summary.json),
[`lpr.json`](../../.tmp/platform-lpr-1024p-5fps-final/lpr.json),
[`runtime-trace.json`](../../.tmp/platform-lpr-1024p-5fps-final/runtime-trace.json) và
[`runtime-evidence.json`](../../.tmp/platform-lpr-1024p-5fps-final/runtime-evidence.json).
Run có đủ ba marker theo validator nhưng round 3 bị lệch nội dung nguồn: scorer ghi
`source_time=0,51 s` trong khi frame thực tế đã ở đoạn rental van khoảng 9,5–11,2 giây. Vì vậy
`measurement_valid=false` theo contract Phase 6-0 mới, dù artifact cũ chưa có field này.

Đây là snapshot kiểm chứng, không phải tuyên bố hoàn tất Phase 6-0. Raw artifact là
[`summary.json`](../../.tmp/platform-upstream-realtime/summary.json),
[`lpr.json`](../../.tmp/platform-upstream-realtime/lpr.json) và
[`container.log`](../../.tmp/platform-upstream-realtime/container.log).

## Audit scorer và kết quả hiện tại ngày 10/08/2026

### Kết luận đo lường

Validator cũ thực hiện ba bước gây sai attribution:

1. suy source time bằng `1,5 + frame_time - anchor`, trong đó `frame_time` là thời gian runtime chứ
   không phải PTS video;
2. nhóm toàn bộ record theo `(camera, round, track_id)` rồi gán cả trajectory cho tối đa một
   ground-truth passage bằng IoU lớn nhất;
3. lấy biển xuất hiện nhiều nhất trong mọi `event_published` của cả ba round làm representative.

Khi RTSP buffer/drop frame hoặc track chuyển xe, output đúng có thể bị bỏ khỏi passage và output xe
khác có thể được nhân nhiều lần rồi thắng representative. Do đó kết quả scorer `4/11`, accuracy
`0,364`, precision `0,4`, recall `0,364` của run 1024p là **invalid KPI**. Nó không được dùng để kết
luận model/OCR giảm chất lượng.

### Quan sát thô đã xác minh được

Bảng dưới chỉ cho biết raw OCR đã từng đọc được gì trong ba replay; cột này là debug ceiling, không
phải end-to-end accuracy:

| Ground truth | Raw OCR tốt nhất đã quan sát | Trạng thái chẩn đoán |
| --- | --- | --- |
| `619879` | `619879` | Exact xuất hiện |
| `C98191P` | `C98191P`; một lượt khác `C98191PJ` | Exact xuất hiện |
| `657648` | `G57648`, `KG57648`, `K657648` | Có OCR, sai ký tự |
| `7BN2396` | `7BN2396` | Exact xuất hiện |
| `1073` | Không có output xác minh | Chưa có OCR |
| `3789` | `3789` | Exact xuất hiện |
| `C64457T` | `C64457T` | Exact xuất hiện |
| `3B53567` | `3B53567` | Exact xuất hiện |
| `FKH9211` | `FKH9211` | Exact xuất hiện |
| `XX6755` | Không có output xác minh | Dừng trước plate/OCR |
| `BEE3975` | `BEE3975` | Exact xuất hiện |

Tổng quan chẩn đoán: 8/11 biển từng có raw OCR exact, `657648` đã vào OCR nhưng sai ký tự, còn
`1073` và `XX6755` chưa có raw OCR xác minh. Không con số nào trong ba số này được dùng làm
production precision/recall cho tới khi scorer theo PTS và candidate lineage chạy lại.

Transit có bằng chứng runtime đúng tại
[`runtime frame`](../../.tmp/platform-lpr-1024p-5fps-final/media/passage-evidence/lpr/1786303958.30789-kbfdl5-27bf244c06c3/00080-runtime_frame_object_box.jpg)
và [`plate crop`](../../.tmp/platform-lpr-1024p-5fps-final/media/passage-evidence/lpr/1786303958.30789-kbfdl5-27bf244c06c3/00086-plate_crop.jpg).
OCR `C98191PJ` score `0,9541` đã publish nhưng scorer bỏ vì record nằm ngoài tolerance 0,3 giây
chỉ 78,4 ms. Một round khác đọc exact `C98191P` score `0,9895`.

Với `XX6755`, chỉ có
[`ground-truth midpoint`](../../.tmp/platform-lpr-1024p-5fps-final/mismatches/lpr-chevy-pickup-01.jpg)
và [`funnel cũ`](../../.tmp/platform-lpr-1024p-5fps-final/mismatches/lpr-chevy-pickup-01.json).
Đây không phải runtime crop. Artifact không có `car_crop`, `plate_detector_input`, `plate_crop` hoặc
OCR tensor cho passage này; funnel cũ ghi detector hit 2/3 nhưng track seen, plate detection và OCR
đều 0/3.

Run kết thúc trong 134,162 giây, không restart nhưng còn pending face pipeline `1`, selector depth
`2` và pinned evidence `2`. Đây là runtime-health failure độc lập với accuracy measurement.

### Việc Phase 6-0 phải sửa trong test/report

- Truyền và lưu source PTS cho mỗi frame/candidate; không suy PTS từ wall-clock hoặc RTSP anchor.
- Match one-to-one từng detection/candidate với ground truth tại cùng PTS và bbox.
- Chấm riêng 11 physical passage × 3 round trước khi aggregate.
- Track chạm nhiều xe phải ghi `track_switch`; không gán cả trajectory vào một passage.
- Final KPI dùng đúng final winner của passage/round cùng candidate lineage; không lấy mode của mọi
  publish và không dùng raw best-frame output làm production accuracy.
- Xuất riêng car detection recall, track coverage/switch, plate detection recall, OCR attempted,
  conditional OCR exact, end-to-end precision/recall và wrong/missing publication.
- Khi thiếu PTS/lineage hoặc round alignment sai, ghi `measurement_valid=false`, KPI liên quan là
  `null`; runtime gate vẫn báo riêng.
- Mỗi hàng báo cáo phải link runtime frame, car crop, plate crop và OCR tensor nếu stage đó tồn tại;
  nếu không tồn tại phải ghi rõ stage đầu tiên bị thiếu.

### Root cause passage `XX6755` và hướng xử lý Phase 6-0

`XX6755` không mất ở plate detector hoặc OCR vì chưa bao giờ tới hai tầng đó. Detector đã bắt xe
ở round 1 qua hai frame liên tiếp:

| Source time | Object bbox | Detector score |
| ---: | --- | ---: |
| `10,9563 s` | `[835,0,1222,170]` | `0,5842` |
| `11,1460 s` | `[843,0,1023,131]` | `0,7665` |

Hai bbox có Norfair distance `1,2348`, thấp hơn ngưỡng car `2,5`; replay trực tiếp chuỗi detection
vào cùng tracker xác nhận detection thứ hai tạo initialized track. Tuy nhiên median confidence mới
chỉ `0,67535`, thấp hơn object threshold `0,7`. `TrackedObject` vừa đăng ký còn có
`computed_score=0` và `false_positive=true`; nó cần frame update tiếp theo để trở thành canonical
true-positive Event.

Frame detector tiếp theo trong artifact nhảy tới khoảng `13,7275 s`, tạo gap `2,5815 s` tương
đương 12–13 frame ở 5 FPS. Runtime ghi `car_camera skipped_fps_max=5,1`; detector hit của frame
`11,1460 s` cũng được trace trễ khoảng 2,99 giây và LPR invocation khác trên cùng frame trễ khoảng
8,45 giây. Vì không còn update cho Chevrolet, candidate track hết hạn và không phát sinh runtime
car/plate/OCR evidence. Round 2 chỉ có một detector hit score `0,5212`; round 3 không chấm được vì
alignment sai.

Phase 6-0 xử lý theo thứ tự:

1. thêm source PTS và metric drop/queue age ở capture, detector output, tracker promotion và
   recognition admission;
2. tách `detector_hit`, `track_candidate_seen`, `track_promoted`, `track_rejected` và canonical
   Event trong report, kèm score history/reason;
3. loại blocking recognition/evidence/trace khỏi detect hot path và giữ queue latest/bounded để
   detector duy trì cadence 5 FPS trong annotated passage;
4. đánh dấu measurement invalid nếu passage có PTS gap do starvation, thay vì tính thành detector
   hoặc OCR miss;
5. thêm regression xe vào từ mép trên, bbox biến đổi mạnh, yêu cầu đủ frame promotion và không
   chuyển ownership sang xe bên cạnh;
6. giữ nguyên object threshold `0,7`, `min_score` và LPR threshold trong bước sửa này. Chỉ calibration
   threshold sau khi cadence/lineage hợp lệ, không hạ threshold để ép test pass.

## Code và ranh giới kiến trúc

- `frigate/src/frigate/infrastructure/data_processing/real_time/license_plate.py` khớp hoàn toàn với
  `upstream/dev` commit `2599795ab0fb2c27f3dd7f9ff6f4a9eb857c4c43`; `git diff --numstat` cho
  file này không có output.
- LPR vẫn là processor/model riêng. Event/API/SQLite schema và notification flow không đổi.
- Adapter truyền YUV frame trực tiếp từ `EmbeddingsMaintainer` vào processor LPR.
- LPR hiện OCR và publish đồng bộ theo `variants`/clustering upstream. Nó không chạy qua
  passage-end rolling top-3/best-result coordinator của Phase 6-0.
- Face vẫn dùng evidence, quality selector và recognition lifecycle hiện có.

Sau artifact baseline, debug adapter xác nhận một sai lệch so với upstream: LPR từng được gọi từ raw
detection-frame objects trong khi expiry lại dùng canonical Event ID. Call boundary đã được sửa để
LPR nhận canonical tracked-object Event update và không còn nhận song song từ detection frame;
logic trong realtime processor không đổi và vẫn diff bằng 0 với upstream. Contract test sau sửa đạt
10/10.

Replay sau adapter fix nằm tại
[`summary.json`](../../.tmp/platform-upstream-event-owner/summary.json) và kết thúc trong
`116,234 giây`. Passage recall/precision đạt `1,0/1,0`, nhưng exact result giảm còn 3/11:
`C98191P`, `FKH9211`, `BEE3975`. Recognition accuracy/precision/recall là
`0,273/0,6/0,273`; overall vẫn `accepted=false`. Raw trace có 44 `ocr_result` so với 23 ở
baseline, nhưng chỉ năm recognition publish được validator tính. Các output đáng chú ý gồm `3789`
ở score tối đa `0,891`, `C64457T` ở `0,900`, `657643` thay cho `657648`, và `6755` thay cho
`XX6755`. Điều này cho thấy canonical ownership đã loại wrong-passage publish trong tập đo nhưng
chưa cải thiện OCR/representative accuracy.

Replay kiểm tra `lpr.recognition_threshold: 0.75` nằm tại
[`summary.json`](../../.tmp/platform-upstream-event-owner-threshold-075/summary.json). Exact result
không tăng, vẫn 3/11. Audit lineage sau run xác nhận raw report có false attribution của validator:
cùng track `FKH9211` được gán đúng vào `lpr-07` ở đầu quỹ đạo nhưng bị gán sang passage `XX6755`
khi xe di chuyển xuống trái. Ba canonical Event trong SQLite đều giữ đúng ID xe và
`recognized_license_plate: FKH9211`; production không gắn biển này vào Event của xe `XX6755`.
Tổng cộng 11 khóa `camera/round/track_id` bị validator cũ chia qua nhiều ground-truth passage. Phép
chấm đã được sửa để khóa toàn quỹ đạo vào tối đa một passage, fail-closed nếu hai passage cùng khớp
mạnh và không dùng OCR text để attribution. Offline re-score đưa multi-passage track về 0: run `0.9`
đạt 3 TP/5 publish, precision `0,6`, passage precision `1,0`; run `0.75` vẫn 3 TP nhưng 7 publish,
precision `0,429`, passage precision `0,917`. Vì `0.75` không thêm true positive và tăng false
publish, deployment đã trả về `0.9`.

Audit tiếp theo phát hiện một lỗi production độc lập: Event `end` mang `frame_name` hiện tại nhưng
vẫn giữ `frame_time`/bbox cuối của object đã biến mất. Maintainer trước đây tiếp tục chạy LPR trên
cặp dữ liệu lệch này; cùng `track_id/frame_time/object_box` đã tạo hai plate bbox khác nhau trong
trace. Adapter hiện chỉ cho LPR xử lý Event `start/update`; Event `end` chỉ đi qua finalization để
`expire_object()`. Regression contract sau sửa đạt 11/11. Replay tại
[`summary.json`](../../.tmp/platform-upstream-end-guard-threshold-075/summary.json) xác nhận duplicate
`track_id/frame_time` ở `plate_detected` giảm từ 2 xuống 0; duplicate OCR/publish vẫn bằng 0. Run
này mất một detection round trên gần toàn fixture, passage recall/precision chỉ `0,818/0,818` và
exact result `2/11`, nên không được dùng để kết luận guard làm giảm OCR. Nó chỉ là bằng chứng trực
tiếp rằng stale end-frame không còn đi tới plate detector.

Final replay với end guard, threshold `0.9` và đủ 3/3 anchor nằm tại
[`summary.json`](../../.tmp/platform-upstream-final-fixed/summary.json). Validator trajectory-lock
được hoàn thiện thêm để ưu tiên bbox ở tầng `event_published → ocr_result → plate_detected` thay vì
để bbox detector cũ ghi đè recognition evidence. Offline re-score cuối có multi-passage track bằng
0, passage precision `1,0`, recognition accuracy/precision/recall `0,273/0,6/0,273` và 3/11 exact:
`657648`, `FKH9211`, `BEE3975`. `BEE3975` thuộc đúng `lpr-06`; không còn output của track này trong
`XX6755`. Passage recall còn `0,909` vì `BEE3975` chỉ có track ở 1/3 round, còn end-event duplicate
ở plate/OCR/publish đều bằng 0.

## Kết quả baseline trước adapter fix

| Chỉ số | Kết quả hiện tại |
| --- | ---: |
| Acceptance tổng | Fail |
| Passage có detection | 11/11 |
| Passage recall | 1,000 |
| Passage precision | 0,917 |
| Recognition publish count do validator tính | 8 |
| Exact true positive | 4/11 |
| Recognition accuracy | 0,364 |
| Recognition precision | 0,500 |
| Recognition recall | 0,364 |
| Tổng thời gian | 125,009 giây |
| Restart/traceback | 0/0 |

## Kết quả từng lượt xe

| Passage | Ground truth | Representative hiện tại | Kết luận |
| --- | --- | --- | --- |
| `lpr-01` | `619879` | Không có | Sai: không có OCR result |
| `lpr-transit-01` | `C98191P` | `C98191P` | Đúng |
| `lpr-02` | `657648` | `657648` | Đúng |
| `lpr-trailer-pickup-01` | `7BN2396` | Không có | Sai: không có OCR result |
| `lpr-red-suv-01` | `1073` | Không có | Sai: không có OCR result |
| `lpr-service-van-01` | `3789` | `C64457` | Sai: output thuộc xe/passage khác |
| `lpr-rental-van-01` | `C64457T` | `C64457` | Sai: thiếu ký tự cuối |
| `lpr-05` | `3B53567` | `3853567` | Sai representative; output đúng `3B53567` có xuất hiện một lần |
| `lpr-07` | `FKH9211` | `FKH9211` | Đúng |
| `lpr-chevy-pickup-01` | `XX6755` | Không có | Sai: plate detection không đủ để tạo OCR result |
| `lpr-06` | `BEE3975` | `BEE3975` | Đúng |

## Diễn giải đúng phạm vi

Kết quả baseline này chứng minh test không bỏ sót 11 lượt xe: tất cả đều có detection. Nó cũng chứng minh
việc copy upstream đã cải thiện exact result lên 4/11 và đọc đúng `657648`, nhưng chưa giải quyết
track/Event ownership và representative selection. Các output `C64457` lặp qua hai passage và việc
`3B53567` xuất hiện nhưng không thắng representative là hai dấu hiệu trực tiếp cần xử lý ở adapter
vòng đời, không phải lý do để đổi ground truth.

Các gate Face, cleanup và giới hạn dưới 119 giây trong summary vẫn fail. Do LPR đang bypass
evidence/coordinator, metric lifecycle/top-3 trong artifact không được dùng để tuyên bố LPR đạt
Phase 6-0.

## Runtime crop evidence cho acceptance debug

Ảnh trong thư mục `mismatches/` chỉ là frame midpoint từ video nguồn với bbox ground truth; nó không
được xem là crop runtime. Acceptance test hiện bật writer riêng qua `PASSAGE_EVIDENCE_DIR` và lưu
theo từng LPR invocation:

- runtime frame nguyên bản và cùng frame có `object_box`;
- car crop và ảnh 2x thực sự đưa vào plate detector;
- plate detector box, score, area, threshold, accept/reject reason và plate crop;
- plate input cho OCR, từng text crop, tensor OCR có thể quan sát;
- text, character scores, mean score, threshold và quyết định cuối.

Mỗi artifact có relative path, byte size và SHA-256 trong `evidence.jsonl`. Validator gán evidence về
physical passage bằng `track_id + frame_time + bbox`, kiểm integrity, yêu cầu đủ evidence cho cả 11
passage và fail gate `lpr_runtime_evidence` nếu thiếu stage, thiếu file, sai hash, vượt bound hoặc thiếu
passage. Báo cáo tổng hợp nằm ở `runtime-evidence.json`; writer chỉ được bật trong acceptance qua biến
môi trường và không thay đổi Event/API/SQLite hay quyết định recognition production.

## Pending eligibility retry ngày 10/08/2026

Canonical Event vẫn là nơi duy nhất cấp ownership cho LPR. Khi lần gọi đầu của một Event bị upstream
chặn bởi `position_changes=0` và `stationary=false`, realtime processor chỉ đăng ký một pending retry
bounded cho đúng `(camera, Event ID)`. Detection frame không được tự tạo passage; nó chỉ được dùng khi
còn chứa đúng ID đã đăng ký, timestamp mới hơn và bbox nằm trong cùng payload với YUV frame đó.
Retry bị dedupe theo timestamp, tối đa 12 frame/3 giây và bị hủy ở Event end, stream epoch reset hoặc
shutdown. Tombstone theo Event ID ngăn Event update cũ mở lại một retry đã resolve/exhaust. Trace mới
gồm `eligibility_retry_scheduled/attempted/resolved/cancelled`.

Contract test cho adapter/evidence đạt `49 passed`; Ruff `E9/F/I` và `compileall` đạt. Acceptance chạy
bằng source overlay, không build Docker. Run debug đầu tại
`.tmp/platform-pending-lpr-retry/summary.json` chứng minh riêng `lpr-01` đi từ ba lần dừng trước gate
sang hai round có `lpr_eligible` và `plate_detected`. Ảnh thực tế là đúng biển `619879`; plate crop
không nhầm xe nhưng mờ và OCR trả `text_detector_empty`, nên vẫn không publish.

Run cuối sau tombstone và manifest-quiescence nằm tại
`.tmp/platform-pending-lpr-retry-final/summary.json`: evidence gate đạt, 167 invocation/832 artifact,
81.035.205 byte, đủ 11/11 passage, không lỗi integrity và không restart. Không Event ID nào bị schedule
retry hai lần. Tuy nhiên run này vẫn `accepted=false`: exact LPR chỉ 3/11 (`657648`, `C64457T`,
`FKH9211`), accuracy/precision/recall `0,273/0,375/0,273`; `lpr-01` chỉ xuất hiện ở round 3 và pending
hết 3 giây trước khi có detection frame cùng ID để retry. Vì vậy fix này đóng lỗi pre-gate khi Event và
detection còn overlap, nhưng chưa xử lý Event-delivery lag dài hơn evidence window và chưa cải thiện
được OCR trên crop mờ. Hai vấn đề đó không được ghi thành pass hoặc quy hết cho model.

Validator cũng chờ `evidence.jsonl` ổn định trước khi chấm. Điều này loại lỗi giả từng xảy ra khi
manifest được đọc sau `plate_crop` nhưng trước record `ocr_result` của cùng invocation.

## Addendum: ba quick-run sau khi sửa SHM/cadence ngày 10/08/2026

Bộ tổng hợp mới nằm tại `.tmp/platform-xx6755-quick-runs-summary.json`; ba artifact tương ứng là
`.tmp/platform-xx6755-quick-1/`, `-quick-2/` và `-quick-3/`. Cả ba run dùng cùng fixture, cùng
image `camera-frigate@sha256:c441b7be5cdd2410ea388761f13b5b11f024b357206a3ec0fdbc4f8cecef8d63`
và runtime budget mới `150 s`.

| Run | Runtime | LPR exact | LPR recall | Face accuracy/recall | XX6755 | Accepted |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | 120,262 s | 1/11 | 0,0909 | 0/0 | Không | false |
| 2 | 108,769 s | 1/11 | 0,0909 | 0/0 | Có OCR/publish | false |
| 3 | 120,877 s | 1/11 | 0,0909 | 0/0 | Có OCR/publish | false |

Run 3 đạt cleanup (`idle`, pinned evidence và selector về 0), nhưng recognition vẫn không đạt.
Đặc biệt, XX6755 có raw OCR/publish ở run 2/3 nhưng không được scorer giữ thành canonical result
của `lpr-chevy-pickup-01`; đây là lỗi ownership/representative, không phải thiếu detector hit.
Kết quả mới không thay thế snapshot baseline ngày 09/08/2026; nó là bằng chứng follow-up cho thấy
cadence/runtime đã cải thiện nhưng Phase 6-0 LPR best-result production path vẫn chưa hoàn tất.
