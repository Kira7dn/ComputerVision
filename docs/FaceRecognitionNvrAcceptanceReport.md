# Báo cáo nghiệm thu Face Recognition cho NVR

Ngày cập nhật: 07/08/2026

## 1. Kết luận

Pipeline face recognition đã đạt mức sẵn sàng để chạy **production có kiểm soát với 6 camera**, khi chức năng nhận dạng được dùng để gắn tên sự kiện, tìm kiếm, review và gửi thông báo trên NVR.

Kết quả chưa đạt toàn bộ SLA kỹ thuật nghiêm ngặt ban đầu. Vì vậy không sử dụng kết quả hiện tại cho access control, mở cửa tự động hoặc quyết định an ninh bắt buộc phản hồi dưới một giây.

Cấu hình vận hành được khuyến nghị:

- 6 camera, 2 detector ONNX, CPU limit 10.
- Một FFmpeg publisher cho file replay dùng chung.
- CUDA là backend nghiệm thu; không sử dụng TensorRT trong image hiện tại.

Ngay sau benchmark, baseline từng được khôi phục về 7 camera/CPU 8. Runtime hiện tại đã được chủ động thu gọn còn hai camera: `face_camera` dùng video person cũ và `car_camera` dùng video car riêng. Thay đổi này diễn ra sau benchmark nên không làm thay đổi các số liệu 6–7 camera trong báo cáo.

## 2. Phạm vi đã triển khai

- Face capture chạy theo track với cadence và số track bounded.
- Candidate store latest-only, scheduler công bằng giữa camera và batch recognition tối đa 4 candidate.
- ArcFace dùng một recognition executor tập trung; YuNet và preprocessing dùng worker bounded.
- Snapshot, tên, bbox và identity chỉ được commit sau transaction media thành công.
- Unknown chỉ tạo training attempt; không phát identity giả.
- Có structured metrics cho candidate, batch, embedding, end-to-end latency và commit state.
- Runtime replay fan-out một publisher thành nhiều camera logic qua MediaMTX.

Model, face library, threshold, voting, public API và DB schema nghiệp vụ không thay đổi.

## 3. SLA strict

| Tiêu chí | Mục tiêu |
|---|---:|
| Camera/process FPS từng camera | >= 4,5 |
| First recognition attempt | <= 750 ms |
| Xác nhận sau hai frame phù hợp | <= 1.500 ms |
| Embedding wall-time | < 200 ms |
| Detector inference | < 200 ms |
| Pending cuối phiên | 0 |
| Restart, reconnect, stall mới | 0 |
| Snapshot failure/camera mismatch | 0 |
| RAM Frigate | <= 7 GiB |
| SHM | < 70% |

## 4. Kết quả benchmark

Mỗi lượt strict chạy 300 giây sau warmup 60 giây. Các số latency dưới đây là giá trị cực đại dùng để nghiệm thu, không phải trung bình.

| Cấu hình | Process FPS min | First max | Confirmed max | Embedding max | Detector max | RAM max | SHM | Kết quả |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 7 camera / CPU 8 | 4,3 | 1.700 ms | 2.320 ms | 836 ms | 17,01 ms | 6,07 GiB | 46% | Không đạt |
| 7 camera / CPU 10 | 3,6 | 2.449 ms | 3.204 ms | 1.241 ms | 27,70 ms | 6,06 GiB | 46% | Không đạt |
| 6 camera / CPU 8 | 4,8 | 1.958 ms | 1.814 ms | 825 ms | 16,65 ms | 5,68 GiB | 39% | Trượt latency |
| 6 camera / CPU 10 | 4,8 | 1.286 ms | 1.660 ms | 681 ms | 25,73 ms | 5,65 GiB | 39% | Tốt nhất, trượt latency |

Ở cấu hình tốt nhất 6 camera/CPU 10:

- Camera/process FPS, detector, RAM và SHM đều đạt.
- Không có container restart, reconnect hoặc stall mới trong cửa sổ đo.
- Pending cuối phiên bằng 0.
- Snapshot failure và camera mismatch bằng 0.
- Chỉ có một replay publisher và live frames thay đổi thực sự.
- First attempt vượt SLA 536 ms; confirmed vượt SLA khoảng 160 ms; embedding vượt SLA 481 ms.

Việc tăng từ CPU 8 lên CPU 10 cải thiện rõ latency ở 6 camera, nhưng không đưa toàn bộ chỉ số qua strict SLA. Với 7 camera, tăng quota CPU không cải thiện kết quả trong lượt đo này.

## 5. Quyết định production NVR

Kết quả được chấp nhận cho **pilot/production NVR có kiểm soát** vì độ trễ end-user khoảng 1,3–1,7 giây vẫn phù hợp với review, tìm kiếm và notification không tức thời.

Điều kiện vận hành:

- Chỉ bật tối đa 6 camera cho face recognition trên cấu hình phần cứng hiện tại.
- Giữ CPU limit 10 và hai detector ONNX.
- Theo dõi restart, pending count, first/confirmed latency, snapshot failure, RAM và SHM.
- Nếu face recognition tham gia access control hoặc cảnh báo bắt buộc dưới một giây, trạng thái phải được coi là chưa đạt.
- Không bật camera thứ bảy trước khi có benchmark mới đạt hoặc thay đổi kiến trúc xử lý contention.

## 6. Kiểm chứng và giới hạn còn lại

Đã kiểm chứng:

- Overlay build thành công: `camera-frigate:overlay-eaa31bb4857d`.
- Image digest: `sha256:eaa31bb4857d210aa84ffa77dc11afa6f718b3335800d1eb81ee5af0b717107b`.
- Doctor hợp lệ cho baseline 7 camera.
- Runtime deployment config chấp nhận CPU 10 và launcher từ chối CPU limit vượt năng lực Docker.
- Runtime được dừng sạch sau benchmark; không còn container Camera/Frigate chạy.
- `git diff --check` không phát hiện lỗi whitespace.

Chưa hoàn tất:

- Soak 60 phút chưa chạy theo quyết định hoãn test dài.
- UI smoke cuối cùng không chạy lại vì không có cấu hình nào đạt strict SLA; lượt UI trước đó đã kiểm tra `/faces`, `/review`, `/explore` mà không có console error.
- Bộ 42 face unit test đã đạt ở lượt triển khai trước. Lần rerun hiện tại bị chặn ở collection do Python host thiếu `pathvalidate`/`ruamel` và runtime image không chứa pytest; không có assertion nào chạy thất bại.

Trước khi bàn giao production chính thức, cần chạy soak 60 phút trên 6 camera/CPU 10 và xác nhận không tăng dần staging, journal, track/candidate, RAM hoặc SHM.

## 7. Artifact và cách chạy lại

Báo cáo benchmark local, không đưa vào Git:

- `.tmp/runtime/face-acceptance-7cam.json`
- `.tmp/runtime/face-acceptance-7cam-cpu10.json`
- `.tmp/runtime/face-acceptance-6cam-cpu8.json`
- `.tmp/runtime/face-acceptance-6cam-cpu10.json`

Lệnh validator:

```powershell
python tools\validate_face_replay.py `
  --seconds 300 `
  --warmup 60 `
  --interval 5 `
  --output .tmp\runtime\face-acceptance-6cam-cpu10.json
```

Bước tối ưu tiếp theo nếu cần đạt strict SLA là cô lập CPU preprocessing/recognition khỏi contention của `embeddings_manager`. Không tiếp tục hạ threshold, cadence, FPS hoặc voting để làm đẹp số benchmark.
