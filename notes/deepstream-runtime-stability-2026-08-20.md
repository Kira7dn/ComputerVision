# Ổn định DeepStream runtime ngày 2026-08-20

## Phạm vi

Đợt xử lý này chỉ tập trung vào độ ổn định của runtime hiện tại trong
`deepstream_safety/`. Không thay model, không đổi topology nhận diện và không đưa
Frigate trở lại pipeline.

Ba lỗi được xử lý:

1. `track_id` của người trên `camera_dahua` bị tạo lại gần như ở mỗi frame, làm
   lịch sử smoking classifier luôn chỉ có một mẫu và không thể xác nhận theo thời
   gian.
2. Hai camera mock thường chết ở worker epoch 1 rồi được supervisor khởi động lại
   ở epoch 2, khiến dashboard khởi đầu chập chờn và phải chờ reconnect.
3. Bbox Dahua nháy xanh/đỏ giữa `person` và `SMOKING` dù smoking track vẫn đang
   confirmed.

## Nguyên nhân đã xác nhận

### 1. Điều kiện chống nhảy mép từ chối chính bbox hợp lệ

Tracker Python có hàm phát hiện một track cũ ở một mép và detection mới ở mép đối
diện để tránh nối nhầm hai passage. Bbox người của Dahua cao gần toàn bộ khung hình,
đồng thời chạm mép trên và mép dưới. Điều kiện cũ coi bbox này là một lần chuyển
từ mép trên sang mép dưới ngay cả khi bbox giữa hai frame gần như trùng nhau.

Hậu quả quan sát được trước khi sửa:

- ID tăng liên tục sau mỗi chu kỳ phân tích.
- `smoking_score_histories` chứa nhiều ID, mỗi ID chỉ có một score.
- Classifier vẫn chạy và có score, nhưng temporal confirmation không thể hoàn tất.

### 2. Readiness mock RTSP chỉ là một khoảng sleep cố định

Worker khởi chạy FFmpeg publisher, sleep 4 giây rồi chỉ kiểm tra tiến trình FFmpeg
còn sống. Khi ba model cùng khởi tạo, FFmpeg có thể vẫn sống nhưng chưa phát được
frame lên MediaMTX.

Trong run cũ, reader kết nối `face_mock`/`safety_mock` lúc 16:07:02 nhưng publisher
chỉ online lúc 16:07:33. Hai worker nhận lỗi source và supervisor phải tạo epoch 2.

### 3. Overlay bị hết hạn theo FPS mặc định sai

Smoking classifier chạy bất đồng bộ mỗi khoảng 400 ms, nhưng output renderer chỉ
giữ kết quả tối đa bốn frame dựa trên FPS mặc định 5. Dahua thực tế phát nhanh hơn
nên overlay bị xóa trước khi inference kế tiếp hoàn tất. Engine vẫn confirmed,
nhưng renderer luân phiên vẽ bbox xanh `person` và bbox đỏ `SMOKING`.

## Cách giải quyết

### Tracker

- Tách các hàm hình học thuần sang `deepstream_safety/tracking.py` để kiểm thử độc
  lập trên Windows.
- Chỉ đánh dấu chuyển mép khi bbox cũ nằm độc quyền ở một mép và bbox mới nằm độc
  quyền ở mép đối diện.
- Bbox kéo dài qua cả hai mép không còn bị xem là một lần nhảy mép.
- Giữ nguyên thuật toán matching, ngưỡng detector, model và kiến trúc
  `person -> track -> person ROI -> smoking classifier`.

### Mock publisher

- Thêm `deepstream_safety/mock_input.py` dùng `ffprobe` để chờ RTSP có video stream
  đọc được, thay cho sleep 4 giây.
- Nếu FFmpeg chết trước readiness, worker fail ngay với nguyên nhân rõ ràng.
- Theo dõi FFmpeg trong toàn bộ vòng đời của cả mock loop. Nếu publisher chết,
  worker dừng để supervisor phục hồi thay vì tiếp tục tồn tại với một live stream
  đã chết.

### Giữ overlay giữa các chu kỳ inference

- Thay freshness theo số frame bằng thời gian monotonic.
- Trạng thái smoking đã confirmed được giữ theo lifecycle của `track_id` cho đến
  khi `SmokingBehaviorEngine` thực sự clear; renderer không được tự hạ nó về
  `person` bằng TTL.
- TTL 2 giây chỉ dùng cho fire/smoke môi trường để không giữ một detection camera
  level đã stale khi analysis thực sự ngừng cập nhật.
- Chỉ thay chính sách render; không thay score, threshold, temporal confirmation,
  tracker hoặc event lifecycle.

### Acceptance an toàn

- Thêm override `DEEPSTREAM_NOTIFICATIONS_ENABLED=false` chỉ dành cho acceptance
  runtime, để kiểm tra live mà không gửi cảnh báo thật.
- Override không thay đổi mặc định trong `config.yaml`; run cuối được khởi động lại
  với notification bật đúng cấu hình thật.

## Kết quả kiểm chứng

Kiểm thử và static checks:

- 21 unit tests pass.
- Ruff pass.
- Python compileall pass.
- PowerShell parser pass cho `start.ps1`.
- `git diff --check` pass.

Acceptance cô lập `20260820T092003-ef825101`:

- Cả ba camera có heartbeat frame mới và API báo ready.
- `camera_face`, `camera_safety`, `camera_dahua` đều giữ worker epoch 1.
- Không còn lỗi `No supported authentication protocol` trong phần log của run.
- Dahua giữ nguyên `track_id=1` qua chuỗi 10 mẫu; smoking history của ID này tích
  đủ bốn score thay vì một score trên nhiều ID khác nhau.
- Trình duyệt thật báo cả ba video `readyState=4`, trạng thái `Live`, `currentTime`
  tiếp tục tăng sau 15 giây; console có 0 error và 0 warning sau reload.

Run cấu hình thật trước bản sửa overlay `20260820T092612-65019ad2`:

- Notification được bật lại.
- Cả ba camera ready, có frame mới và giữ epoch 1 sau cold start.
- Runtime đang phục vụ dashboard tại `http://127.0.0.1:18080/dashboard.html`.

Run sau bản sửa overlay `20260820T093352-93dd0e01`:

- Cả ba worker khởi động ở epoch 1; Dahua ready và không có `analysis_error`.
- Analysis Dahua cập nhật trong khoảng 0,25–0,39 giây, thấp hơn giới hạn giữ
  overlay 2 giây nên renderer không còn tự xóa trạng thái smoking giữa hai chu kỳ.
- Clip kiểm tra cùng renderer trên `camera_safety` cho thấy frame có person object
  và smoking active được vẽ đỏ; các frame không có bbox là detector không trả
  person object, không phải bị đổi về bbox xanh do cache hết hạn.
- Khi kiểm tra Dahua trong lúc người dùng đang hút, engine vẫn confirmed với chuỗi
  score 60,5–66%, nhưng analysis có lúc trễ 3,8 giây. Điều này chứng minh TTL 2
  giây vẫn có thể xóa nhầm overlay. Bản sửa cuối loại TTL khỏi smoking track state;
  chỉ fire/smoke camera-level còn dùng TTL.

## Những việc không được coi là đã giải quyết trong đợt này

- Chất lượng model fire/smoke và các false positive như tai bị nhận thành fire là
  vấn đề model/dataset/threshold riêng, không được che bằng bypass trong thay đổi
  ổn định này.
- Zalo đang trả lỗi hết quota ngày; đây không phải lỗi pipeline hoặc retry runtime.
- Acceptance trình duyệt ở trên chứng minh một cửa sổ phát liên tục ngắn, chưa phải
  soak test nhiều giờ.
