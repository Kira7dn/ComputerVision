# Báo cáo nghiệm thu native LPR hai camera ở 720p

## Kết luận

Kết quả: **không đạt nghiệm thu và đã rollback**.

Native LPR đã chạy đúng data flow `car` → crop ROI xe → YOLOv9 plate detector → PaddleOCR trên GPU, nhưng một vehicle passage lặp lại chỉ đạt độ nhất quán OCR 66,7%, thấp hơn ngưỡng 80%. Không hạ detection/recognition threshold, không đổi detector chính, không tăng độ phân giải và không đổi sang dedicated LPR.

Sau phép thử, cấu hình đã được khôi phục về `lpr.enabled: false`; hai camera đã được restart và đang chạy. Detector chính vẫn là `models/yolov9-t-320.onnx`.

Artifact đầy đủ: `.tmp/runtime/lpr-acceptance-2cam-720p.json`.

## Cấu hình đã thử

```yaml
lpr:
  enabled: true
  model_size: small
  device: GPU
  detection_threshold: 0.7
  recognition_threshold: 0.9
  min_plate_length: 4
  min_area: 1000
  debug_save_plates: true

cameras:
  face_camera:
    lpr:
      enabled: false
    face_recognition:
      enabled: true
    detect:
      width: 1280
      height: 720
      fps: 5

  car_camera:
    type: generic
    lpr:
      enabled: true
      min_area: 1000
    face_recognition:
      enabled: false
    objects:
      track: [car]
    detect:
      width: 1280
      height: 720
      fps: 5
```

`license_plate` không được thêm vào detector chính và camera car không chuyển sang `type: lpr`.

## Model và data flow

Model cache nằm trong persistent `/config/model_cache`. Model state qua WebSocket đều là `downloaded`:

- `yolov9-256-license-plates.onnx`
- `detection_v5-small.onnx`
- `classification.onnx`
- `recognition_v4.onnx`

Mỗi model được tải đúng một lần trong lần khởi động đầu. File ký tự `ppocr_keys_v1.txt` cũng được cache nhưng không tính là model.

Runtime source contract của container được kiểm tra: detector nhận crop `rgb[top:bottom, left:right]`, sau đó đổi `plate_box` từ tọa độ crop về tọa độ frame bằng offset `left/top`. Có 264 live LPR updates; tất cả `plate_box` hợp lệ trong frame 1280×720. Detection và OCR metrics đều xuất hiện, không có lỗi model hoặc execution provider.

## OCR consistency

Nguồn replay car dài 15,018 giây. Cửa sổ 300,017 giây quan sát khoảng 19,98 vòng độc lập, có 79 event OCR trong API và 79 record tương ứng trong SQLite. Tất cả kết quả được chấp nhận có score từ 0,9 trở lên.

Các passage chính:

| Passage | Số passage có OCR | Chuỗi đại diện | Consistency | Variant |
| --- | ---: | --- | ---: | --- |
| BEE | 7 | `BEE3975` | 100% | `BEE3975`: 7 |
| C644 | 17 | `C64457T` | 94,1% | `C64457T`: 16, `376336`: 1 |
| 385 | 14 | `3853567` | 92,9% | `3853567`: 13, `3B5356`: 1 |
| FKH/C981 | 15 | `FKH9211` | **66,7%** | `FKH9211`: 10, `C98191P`: 5 |

Passage FKH/C981 lặp cùng phase của video và cùng trajectory signature, nên đây không phải lỗi gom nhóm đơn thuần. Kết quả này làm fail tiêu chí consistency ≥80%.

Không có ground truth ký tự cho video, vì vậy các số trên chỉ chứng minh tính lặp lại của OCR, không chứng minh độ chính xác ký tự tuyệt đối.

## Tài nguyên và ổn định

Trong cửa sổ 5 phút:

- `face_camera`: camera FPS tối thiểu 5,0; process FPS tối thiểu 4,9.
- `car_camera`: camera FPS tối thiểu 9,5; process FPS tối thiểu 6,2.
- Restart delta: 0 cho Frigate và hai publisher.
- Reconnect delta: 0 cho cả hai camera.
- Stall delta: 0 cho cả hai camera.
- RAM Frigate tối đa: 5.005.784.383 byte, khoảng 4,66 GiB, dưới 7 GiB.
- SHM tối đa: 13,60%, dưới 70%.

Stats schema hiện tại không expose queue depth `pending`; thay vào đó báo cáo đã sample detector/enrichment activity và kiểm tra restart/reconnect/stall delta.

## API, SQLite và UI

- 79/79 event có `recognized_license_plate`, score, camera và timestamp giống nhau giữa API và SQLite.
- Explore hiển thị filter `Recognized License Plates` và event `BEE3975 (97%)`; crop debug được kiểm tra trực quan và đọc được `BEE3975`.
- Face pipeline không bị ảnh hưởng: Face Library hiển thị 96 recent recognitions; API sample có 138/200 person events mang face sub-label, gồm `Joe`, `Daniel`, `Doe`, `Dan`, `Nghia`, `Dacey`, `Anne`, `Daisy` và `Rull`.
- Browser phát sinh 404 ở `/api/review/event/<event-id>` khi mở Tracked Object Details. Plate details vẫn render, nhưng tiêu chí “không console error” không đạt sạch. Đây là lookup Review chung, không phải lỗi model/provider LPR.

## Rollback và cleanup

Cấu hình cuối:

```yaml
lpr:
  enabled: false
```

Runtime cuối đã xác nhận:

- `face_camera` và `car_camera` đều chạy ổn định.
- `car_camera` vẫn là `generic`, chỉ track `car`.
- `face_camera` vẫn bật face recognition.
- Embeddings stats sau rollback chỉ còn face recognition; không còn plate detection/OCR activity.
- Không có log LPR sau restart rollback.

1.934 debug crop của phiên thử đã được đưa khỏi media production vào archive có thể phục hồi `.tmp/runtime/lpr-debug-failed-20260807-1324`. Thư mục production `clips/lpr/car_camera` đã được tạo lại rỗng.

## Hướng tiếp theo

Để thử lại mà không phá các guardrail hiện tại, cần cung cấp video/stream có biển rõ hơn hoặc chủ động phê duyệt tăng độ phân giải detect lên 1080p/4K. Mỗi candidate phải chạy lại replay theo vehicle passage và đạt consistency ≥80% trước khi bật production. Không nên hạ recognition threshold dưới 0,9 để ép kết quả.
