# Báo cáo audit hiệu năng Dynamic Pipeline

**Ngày audit:** 2026-08-26  
**Phạm vi:** LS-Vision runtime tại `apps/src`, cấu hình production và runtime thực tế trên Jetson Orin Nano  
**Trạng thái:** Đã triển khai; native production acceptance và browser acceptance đều đạt

## 0. Kết quả triển khai ngày 2026-08-26

Đã triển khai theo thứ tự đo lường → transport/overlay → cadence/output:

- bổ sung CPU/RSS hot-path theo process và CPU p50/p95 trong native E2E;
- thêm gate cadence thực tế trên cửa sổ đo 60 giây;
- chuyển DMS và front sang `nvv4l2decoder` nhưng giữ nguyên codec/source Dahua;
- front dùng H.264 all-intra packet-copy, không còn OpenCV decode/BGR copy/x264 encode;
- front worker chạy metadata-only, browser đọc `camera_front_raw` và vẽ overlay SVG từ metadata;
- giảm riêng DMS browser derivative xuống 512×288, 2 Mbps, 4 FPS; input/inference vẫn 1920×1080 ở cadence 10 Hz;
- crop/resize driver ROI còn tối đa 512 px trước FaceMesh, không đưa full-frame 1920×1080 vào MediaPipe;
- FaceMesh chạy tối đa 6.67 Hz và tái sử dụng kết quả ở các tick DMS xen kẽ; DMS policy/executor vẫn 10 Hz;
- sửa scheduler fixed-phase và dùng cùng tolerance ở admission/due/submit;
- đưa RTP/frame timing của packet publisher vào live metadata để browser đo latency và giữ time-plane lock.

Kết quả dev sau warm-up, đo 60 giây:

| Gate | Trước tối ưu | Sau tối ưu |
|---|---:|---:|
| Hot-path CPU p95 | khoảng 290% | **189.1%** |
| Front publisher CPU | khoảng 45–55% | **3.9%** |
| DMS cadence | khoảng 6.2/10 Hz | **9.39/10 Hz** |
| Front cadence | khoảng 13.6/20 Hz | **19.71/20 Hz** |
| Front output encoder | `x264enc` | **disabled** |
| Front input decoder | software | **`nvv4l2decoder`** |

Browser development acceptance:

- 5/5 video có `readyState=4`, front raw 960×540;
- overlay metadata được browser render (đã quan sát 17 SVG segments);
- 4/4 synchronized mock registered và playing;
- `locked=true`, reference live latency khoảng 252 ms;
- p95 drift **24.6 ms**, max drift **98.2 ms**, reconnect count 0.

Production release `release-20260826-153302`, đo sau warm-up trong cửa sổ 60 giây:

| Gate production | Kết quả | Ngưỡng |
|---|---:|---:|
| Hot-path CPU p50 | **181.8%** | — |
| Hot-path CPU p95 | **187.1%** | ≤ 190% |
| Pipeline CPU p95 | **183.1%** | ≤ 500% |
| Pipeline RSS | **2480.8 MB** | ≤ 3500 MB |
| DMS cadence | **9.384/10 Hz (93.84%)** | ≥ 90% |
| Front cadence | **19.135/20 Hz (95.68%)** | ≥ 90% |
| Analysis queue depth | **0 / 0** | ≤ 1 |
| DMS latency | **18.9 ms** | ≤ 500 ms |

Native report có `accepted=true`, không có error. Browser thật tại
`http://vision.local` xác nhận:

- 5/5 video phát với `readyState=4`;
- DMS derivative thực tế 512×288; front raw 960×540;
- front overlay được render bằng 31 SVG segments tại thời điểm kiểm tra;
- nhóm `vehicle_surround` có 4/4 registered và playing, `locked=true`,
  `serverLocked=true`;
- p95 drift **51.4 ms**, max drift **52.1 ms**, reference latency **353 ms**,
  reconnect count 0.

Source release được tạo từ working tree đang có thay đổi của task
(`source_dirty=true`); đây là trạng thái provenance, không làm report acceptance
thành thất bại.

TensorRT đã được thử trên development nhưng engine build thất bại tại fusion
`node_conv2d_1 + node_gelu_1`; provider order vì vậy vẫn là CUDA trước TensorRT.
Không promote một provider chưa qua model acceptance.

## 1. Mục tiêu

Tìm hướng cải thiện hiệu năng có giá trị cao nhất trong khi vẫn bảo đảm:

- cấu hình function/model độc lập theo từng camera;
- selective reload, không restart camera không liên quan;
- latest-only processing, không tích backlog;
- giữ nguyên cơ chế đồng bộ thời gian của bốn camera 360 mock video;
- không thay đổi ownership giữa LS-Vision và `services/camera-server/`.

## 2. Kết luận điều hành

Cải tiến có giá trị cao nhất là **tách video transport khỏi overlay**, để browser nhận compressed stream trực tiếp từ MediaMTX và vẽ overlay từ metadata. Khi đó vision worker chỉ decode để inference và không còn phải software encode toàn bộ video đầu ra.

Đây là ưu tiên cao nhất vì Jetson hiện tại là **NVIDIA Jetson Orin Nano**, không cung cấp GStreamer element `nvv4l2h264enc`. Cả DMS và camera front vì vậy đang fallback sang `x264enc`, tạo tải CPU liên tục trên mọi frame, không phụ thuộc cadence inference.

Kiến trúc đích:

```text
Compressed source ───────────────> MediaMTX ───────> Browser video
        │                                                + overlay canvas
        └──> hardware decode ──> inference ──> metadata ────────┘
```

Việc chuyển sang hardware decode có giá trị cao thứ hai và nên được thử trước vì phạm vi thay đổi nhỏ hơn. Tuy nhiên, nếu vẫn giữ `x264enc`, phần chi phí lớn nhất chưa được loại bỏ.

## 3. Bằng chứng runtime

Snapshot production tại thời điểm audit:

| Chỉ số | Giá trị |
|---|---:|
| Host CPU | 53.1% trên 6 core |
| CPU của hai vision worker | 234.8% |
| RSS của hai vision worker | 2631 MB |
| CPU mock publisher camera front | khoảng 55% |
| DMS input decoder | `avdec_h265` |
| Front input decoder | `avdec_h264` |
| DMS output encoder | `x264enc` |
| Front output encoder | `x264enc` |
| Analysis queue depth | 0 cho cả hai worker |

Runtime vẫn đáp ứng latency và không có backlog, nhưng cadence xử lý thực tế thấp hơn plan:

| Camera | Source FPS đo được | Processed FPS đo được | Plan công bố |
|---|---:|---:|---:|
| DMS | 10.94 | 6.20 | 10 Hz |
| camera_front | 21.52 | 13.59 | 20 Hz |

Do đó, `queue_depth=0` chỉ chứng minh cơ chế latest-only không tích backlog; nó chưa chứng minh pipeline đạt throughput cấu hình.

## 4. Các phát hiện

### P0 — Software encode là bottleneck lớn nhất

Runtime thử `nvv4l2h264enc`, sau đó fallback sang `x264enc` khi element không tồn tại. Trên Jetson đang triển khai, hardware decoder `nvv4l2decoder` có sẵn nhưng hardware H.264 encoder không có.

Hậu quả:

- mỗi vision worker software encode mọi output frame;
- chi phí encode tồn tại ngay cả khi model chưa đến cadence inference;
- tăng CPU, RAM bandwidth và nhiệt độ;
- thêm một lần color conversion trước output encoder.

### P0 — Performance acceptance chưa kiểm tra throughput thực

Compiler hiện ước lượng inference rate bằng tổng `1 / interval` của các top-level function. Cách tính này chưa bao gồm:

- shared person detector;
- FaceMesh trong DMS;
- YOLO object detector lồng trong DMS;
- decode, resize, CPU copy và encode;
- provider thực tế và chi phí khác nhau giữa TensorRT/CUDA/CPU.

Acceptance production hiện kiểm tra CPU dưới 500%, RAM dưới 3500 MB và queue depth không vượt 1. Nó chưa kiểm tra:

- processed FPS tối thiểu;
- tỷ lệ drop;
- inference p95/p99;
- CPU p95 trong một measurement window;
- GPU utilization;
- chi phí theo từng camera/process.

### P1 — Admission gate làm hụt cadence

Scheduler đặt deadline kế tiếp bằng `now + interval`. Khi frame đến sớm hơn deadline chỉ vài mili-giây, frame bị drop và deadline thường chỉ được phục vụ ở frame tiếp theo, gây hụt cadence đáng kể.

Scheduler đích cần:

- dùng source timestamp hoặc monotonic slot;
- tiến deadline theo slot trước đó thay vì thời điểm frame thực đến;
- coalesce thành latest-only khi worker bận;
- công khai `planned_fps`, `admitted_fps`, `processed_fps` và drop reason.

### P1 — Full-frame CPU copy trước khi crop DMS

DMS map và copy nguyên BGR frame 1920×1080, khoảng 8.29 MB mỗi lần admission, rồi mới crop driver ROI. Đây là chi phí memory bandwidth không cần thiết đối với FaceMesh.

Hướng tối ưu:

- resize/crop trong NVMM trước khi map CPU;
- copy driver ROI riêng cho FaceMesh;
- cấp surface 640×640 riêng cho object detector;
- giữ phép biến đổi tọa độ rõ ràng để overlay và evidence vẫn dùng tọa độ source frame.

### P1 — Mock publisher decode và encode lại video

Camera front mock hiện đi qua:

```text
MP4 → OpenCV decode → BGR bytes copy → Gst buffer copy → x264 encode → RTSP
```

Publisher này sử dụng khoảng 55% CPU. Hướng đích là packet-copy compressed H.264 và tái tạo PTS dựa trên shared epoch. Việc seek/loop vẫn phải sử dụng cùng công thức timeline hiện tại.

### P1 — TensorRT chưa được ưu tiên

Production khai báo `CUDAExecutionProvider` trước `TensorrtExecutionProvider`. Front engine thử từng provider theo đúng thứ tự và dừng ở provider đầu tiên khởi tạo thành công. Runtime vì vậy đang dùng CUDA và chưa thử TensorRT.

TensorRT cần được benchmark trên dev trước khi đổi production vì lần build engine đầu có thể chậm và output cần được kiểm tra sai số.

### P2 — Metadata được dựng lặp lại

Runtime dựng payload metadata lớn trên mọi frame rồi mới áp dụng giới hạn ghi file 250 ms. Một payload gần tương tự tiếp tục được dựng để publish qua ZMQ.

Có thể giảm chi phí bằng cách:

- kiểm tra cadence trước khi dựng payload;
- tạo một immutable metadata snapshot dùng chung;
- tách event transition tức thời khỏi periodic telemetry;
- chỉ serialize khi có subscriber hoặc đến kỳ publish.

## 5. Kế hoạch triển khai đề xuất

### Giai đoạn 1 — Đo đúng và tối ưu ít rủi ro

1. Bổ sung benchmark window tối thiểu 60 giây.
2. Thêm gate `processed_fps >= 90% planned_fps` hoặc ngưỡng riêng theo function.
3. Ghi CPU/RSS p50/p95 theo từng process và GPU utilization.
4. A/B `nvv4l2decoder` cho DMS và front trên dev.
5. Thử TensorRT trước CUDA và cache engine.
6. Sửa admission deadline và metadata throttle.

Điều kiện chuyển giai đoạn:

- không giảm model acceptance;
- không tăng camera latency p95;
- queue depth vẫn tối đa 1;
- selective reload vẫn chỉ restart camera bị thay đổi;
- timeline publisher PID không đổi.

### Giai đoạn 2 — Loại software output encode

1. Bổ sung browser-side overlay chạy song song với burned-in overlay hiện tại.
2. Correlate metadata bằng `camera_id`, `source_epoch`, `frame_number`, source timestamp và PTS.
3. Cho browser đọc compressed passthrough stream từ MediaMTX.
4. Kiểm tra overlay alignment trên video đang phát.
5. Tắt OSD/output encode trong vision worker sau khi acceptance đạt.
6. Giữ burned-in derivative dưới dạng tùy chọn on-demand nếu evidence hoặc downstream bắt buộc cần video đã render.

### Giai đoạn 3 — Giảm copy và tối ưu model

1. Crop/resize trong NVMM trước CPU mapping.
2. Tách DMS FaceMesh ROI khỏi full-frame object detector input.
3. Benchmark TensorRT cho front và DMS object detector.
4. Thay inference budget đơn giản bằng weighted resource plan.

## 6. Bảo vệ đồng bộ bốn camera 360

Performance work không được thay đổi time-plane contract:

- `sync_group`, `sync_period_seconds` và `sync_epoch_seconds` vẫn là source of truth;
- timeline publisher không phụ thuộc vòng đời inference worker;
- model/provider reload không restart mock publishers;
- compressed packet-copy phải phát PTS theo shared timeline, không theo thời điểm process bắt đầu;
- media-only camera không tạo vision worker;
- browser overlay không được điều khiển `video.currentTime`.

Acceptance bắt buộc sau mỗi thay đổi:

| Gate | Ngưỡng |
|---|---:|
| Camera registered/playing | 4/4 |
| Timeline locked | `true` |
| p95 drift | < 100 ms |
| Max drift | < 250 ms |
| Reconnect count | 0 |
| Timeline publisher PID sau model reload | Không đổi |
| Analysis queue depth | ≤ 1 |
| Processed cadence | Đạt ngưỡng function đã cấu hình |

## 7. Tiêu chí xác nhận hiệu quả

Không kết luận tối ưu thành công chỉ dựa trên readiness hoặc queue depth. Báo cáo A/B phải dùng cùng config, video, model, thời gian warm-up và measurement window, đồng thời ghi:

- CPU/RSS p50, p95 và peak;
- GPU utilization và power;
- decode/encode implementation thực tế;
- source/admitted/processed FPS;
- inference latency p50/p95/p99;
- frame drop theo nguyên nhân;
- camera latency;
- timeline drift và reconnect;
- model/event acceptance trước và sau.

Ưu tiên quyết định vẫn là: **loại software output encode trước, sau đó giảm full-frame copy và tối ưu provider/model**.
