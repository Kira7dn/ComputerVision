# Báo cáo vấn đề runtime Camera

**Cập nhật:** 06/08/2026
**Phạm vi:** Frigate, mock stream, `gate_in_camera`, `face_camera`, object detection, LPR/OCR và cấu hình deploy.

## Tóm tắt hiện trạng

Runtime hiện đã chạy được hai mock stream và Frigate healthy. Camera 1 nhận diện được `car`, phát hiện được bbox `license_plate`, và LPR đã từng OCR thành công. Tuy nhiên OCR chưa ổn định: nhiều event có plate bbox nhưng log vẫn báo `No text detected`.

Camera 2 vẫn chạy person detection/face pipeline theo cấu hình riêng. Không có thay đổi nào vào source Frigate hoặc image Docker.

## Trạng thái đã xác minh bằng API

Các số liệu dưới đây được lấy trực tiếp từ Frigate API, không dùng event cũ làm bằng chứng runtime hiện tại:

- `gate_in_camera`: khoảng 10 FPS; detection đã chạy ổn định ở các lần kiểm tra gần đây.
- `face_camera`: khoảng 5 FPS; detection/process vẫn hoạt động.
- ONNX detector: khoảng 18–21 ms ở các mẫu đo.
- LPR pipeline: có lúc chạy khoảng 1–6 lần/giây.
- Camera 1 đã có nhiều event `car` mới và nhiều event có `license_plate` bbox.
- Đã từng OCR thành công các biển số mới:
  - `WC65ZFX` — score `0.9179`
  - `LL6IPZS` — score `0.9264`
  - `BP63LYH` — score `0.9840`
- Ở cửa sổ kiểm tra sau đó, `recognized_license_plate` có thể trở về `0` dù plate bbox vẫn còn. Vì vậy kết quả hiện tại là **OCR có khả năng hoạt động nhưng chưa ổn định**, không phải đã nghiệm thu ổn định.

## Vấn đề LPR/OCR

### Phần đang hoạt động

Pipeline hiện có đủ các tầng:

1. Detector nhận diện `car`.
2. Model custom nhận diện `license_plate` và gắn bbox vào car.
3. Frigate gọi LPR/OCR.
4. Một số crop được OCR thành công.

### Nguyên nhân đã xác định của các lần OCR thất bại

Log LPR chuyên biệt báo lặp lại:

```text
Running plate recognition
No text detected
```

Điều này xảy ra trước bước kiểm tra recognition threshold. Do đó việc giảm threshold không giải quyết được trường hợp OCR không tìm thấy text.

Các plate bbox mới đo được trên stream `1280x720` trước khi nâng nguồn có kích thước rất nhỏ, ví dụ khoảng:

- `22x7 px`
- `31x9 px`
- `60x14 px`
- `67x17 px`

Một số bbox còn chạm mép trái ảnh. Các crop đã OCR thành công thì nhìn rõ ký tự; các crop lỗi thường không đủ pixel, bị lệch hoặc không chứa vùng chữ rõ ràng.

### Cấu hình LPR hiện tại

Baseline debug đã được đưa về theo hướng dẫn Frigate:

```yaml
lpr:
  enabled: true
  debug_save_plates: true
```

Đã bỏ:

- `recognition_threshold: 0.6` — không giữ thay đổi thử nghiệm này.
- `min_area: 100` — bỏ để không lọc crop quá sớm trong giai đoạn debug.
- `enhancement: 3` — bỏ vì enhancement cao có thể làm crop nhỏ bị nhòe/méo; mặc định là `0`.

Logger LPR chuyên biệt đã bật để phân biệt `No text detected`, confidence thấp và lỗi độ dài.

## Model và score plate

Model đang dùng:

```text
/models/logistics-yolov8.onnx
model_type: yolo-generic
```

Model có class `car`, `license_plate` và nhiều class logistics khác. File labelmap runtime gồm cả `car`, `truck`, `van` và `license_plate`.

Một vấn đề kỹ thuật còn tồn tại: score plate trong API đôi lúc lớn hơn `1.0`, ví dụ `1.50` hoặc `1.68`. Đây là hậu quả của bản model đã được chỉnh gain score cho class plate. Nó không làm mất bbox, nhưng làm score không còn là xác suất hợp lệ và có thể gây hiểu nhầm khi đánh giá threshold. Không nên tiếp tục chỉnh gain model trong lúc đang chẩn đoán OCR.

Một số event cũng có hai attribute plate giống hệt nhau. Đây cần được rà soát tiếp ở association/post-processing, nhưng không phải nguyên nhân chính của `No text detected`.

## Độ phân giải và CPU

Video mock camera 1 có nguồn gốc `3840x2160`, nhưng flow cũ trong `start.ps1` ép xuống `1280x720`. Đây là lý do plate còn rất ít pixel.

Đã thử nâng riêng gate stream lên `1920x1080` và cấu hình detect gate tương ứng; camera 2 vẫn giữ `1280x720`. Sau thay đổi, CPU container đo được khoảng `325%`, tương đương khoảng 3.25 core.

Phân rã process tại thời điểm CPU cao:

- `frigate.process:gate_in_camera`: khoảng `86%`.
- `frigate.detector:onnx`: khoảng `77%`.
- tiến trình Frigate chính: khoảng `76%`.
- embeddings/LPR manager: khoảng `41%`.
- camera 2 process: khoảng `21%`.
- FFmpeg gate: khoảng `16%`.

Ngoài ra, process FFmpeg của Frigate vẫn được quan sát đang scale detect về `1280x720`. Cần xác minh lại runtime config sau mỗi lần recreate/restart để chắc chắn lợi ích của nguồn `1920x1080` thực sự đi tới detector, thay vì chỉ tăng chi phí decode và scale.

## Cấu hình deploy và Source of Truth

Trước đây tồn tại hai bản config:

- `deploy/config.yml` trong workspace.
- `E:\Docker\Frigate\config\config.yml` trên runtime volume.

`start.ps1` từng copy bản workspace sang ổ E, gây nguy cơ lệch config và khó biết bản nào là chuẩn.

Đã chuyển sang một SOT duy nhất:

```text
D:\BusinessAnalyze\Camera\deploy\config.yml
```

`docker-compose.yml` mount trực tiếp file này:

```yaml
./config.yml:/config/config.yml:ro
```

`start.ps1` không còn copy config sang ổ E. File config cũ trên ổ E đã được xóa. Docker inspect đã xác minh source mount hiện tại là `deploy/config.yml` với `RW=False`.

Entrypoint vận hành chuẩn là:

```powershell
.\deploy\stop.ps1
.\deploy\start.ps1
```

Không dùng restart thủ công để thay thế flow này khi cần đồng bộ mock publisher và Compose.

## Những thay đổi đã thực hiện

- Xóa `person` khỏi `gate_in_camera`; camera 1 chỉ còn track `car` và `license_plate`.
- Camera 2 vẫn track `person`.
- Xóa thử nghiệm `recognition_threshold: 0.6`.
- Đưa LPR về baseline `enabled + debug_save_plates`.
- Bật logger `frigate.data_processing.common.license_plate: debug`.
- Chuyển config về một SOT duy nhất.
- Tách độ phân giải mock gate và face trong `start.ps1`.
- Nâng thử gate mock lên `1920x1080` để tăng pixel cho plate.

## Những việc chưa được nghiệm thu

- OCR plate ổn định trong một cửa sổ dài.
- Tỷ lệ OCR thành công trên toàn bộ car event.
- Chất lượng/độ chính xác của mọi plate bbox.
- Score plate nằm trong khoảng xác suất hợp lệ `0..1`.
- CPU chấp nhận được sau khi giữ stream gate `1920x1080`.
- Runtime Frigate thực sự sử dụng detect input `1920x1080` thay vì tiếp tục scale nội bộ về `1280x720`.
- Không có duplicate plate attribute trong event.

## Kết luận và hướng xử lý tiếp theo

Không nên tiếp tục giảm threshold OCR hoặc đổi model một cách ngẫu nhiên. Bằng chứng hiện tại cho thấy vấn đề chính là chất lượng/kích thước crop plate và chi phí pipeline khi nâng độ phân giải.

Thứ tự xử lý an toàn tiếp theo:

1. Xác minh runtime config và kích thước frame thực tế sau recreate.
2. Lấy crop LPR của các event mới báo `No text detected`.
3. Nếu crop sai/lệch: sửa plate bbox/model hoặc nguồn mock.
4. Nếu crop đúng nhưng chữ quá nhỏ: chọn độ phân giải/ROI phù hợp và đo lại CPU.
5. Chỉ sau khi OCR có dữ liệu ổn định mới tinh chỉnh threshold, format hoặc replace rules.

## Bảng trạng thái

| Hạng mục | Trạng thái |
|---|---|
| Frigate healthy | Đạt |
| Mock stream gate | Đạt |
| Mock stream camera 2 | Đạt |
| Car detection camera 1 | Đạt |
| License plate bbox camera 1 | Đạt nhưng chất lượng không đồng đều |
| OCR plate | Có lúc đạt, chưa ổn định |
| Face/person pipeline camera 2 | Đang chạy, cần tiếp tục nghiệm thu event thực tế |
| Một SOT config | Đạt |
| CPU sau nâng gate lên 1920x1080 | Chưa đạt/đang đánh giá |
| Runtime detect thực sự ở 1920x1080 | Chưa xác minh |
| Nghiệm thu production | Chưa đạt |
