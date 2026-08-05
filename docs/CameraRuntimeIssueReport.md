# Báo cáo vấn đề runtime Camera

**Ngày:** 05/08/2026  
**Phạm vi:** Frigate, mock stream, gate-in và camera face recognition

## Tóm tắt

Runtime chưa đạt điều kiện nghiệm thu event. Stream vẫn chạy và Frigate healthy, nhưng event object mới không được tạo ổn định. Vì vậy face recognition chưa nhận được đầu vào `person` và chưa thể tạo dataset.

## Vấn đề đã xác nhận

### 1. Gate-in không tạo event mới ổn định

- `gate_in_camera` vẫn nhận stream khoảng 10 FPS.
- FFmpeg publisher và MediaMTX vẫn hoạt động.
- Frame trực tiếp có xe và model chạy độc lập nhận diện được `car`.
- Frigate từng có event `car` cũ.
- Kiểm tra gần nhất không có event gate-in mới theo timestamp hiện tại.
- `detection_fps` của gate-in có thời điểm bằng `0.0`, sau đó chỉ tăng không ổn định.

### 2. Camera 2 không tạo person event

- `face_camera` nhận stream khoảng 5 FPS.
- Frigate face processor khởi tạo thành công.
- ArcFace model đã được tải.
- Config live có `objects.track: [person]` và input có role `detect`.
- API không trả event `person` cho `face_camera`.
- Không có face attempt và không có file trong Face Library.

### 3. Collector chưa có dữ liệu

- Collector đã hỗ trợ ghi `id`, `bbox`, `box`, `region`, thời gian, confidence, `path_data`, attributes, sub-label, tốc độ, góc di chuyển, snapshot/clip flags và `raw_event`.
- Tuy nhiên report chưa có dòng dữ liệu vì Frigate chưa tạo event `person`.

## Bằng chứng kỹ thuật

- Frigate live config xác nhận:

```yaml
face_recognition:
  enabled: true

cameras:
  gate_in_camera:
    enabled: true
    objects:
      track:
        - car

  face_camera:
    enabled: true
    objects:
      track:
        - person
    face_recognition:
      enabled: true
```

- Source Frigate cho thấy face processor chỉ xử lý khi nhận được tracked object có `label: person`.
- Model ONNX chạy độc lập trên frame mẫu nhận diện được người với confidence khoảng `0.92–0.96`, nhưng đây không phải bằng chứng Frigate runtime đã nhận diện được người.
- Snapshot API của cả hai camera trả frame `1280×720`; lỗi không nằm ở việc stream không có hình.

## Nguyên nhân hiện tại

Nguyên nhân nằm ở pipeline object detection/tracking của Frigate với mock stream và model ONNX hiện tại. Motion detector có thể không kích hoạt object detector khi cảnh gần như đứng yên; ngoài ra cần tiếp tục kiểm tra post-processing ONNX trong Frigate.

Face recognition không phải điểm bắt đầu của lỗi. Theo thiết kế Frigate, phải có `person` tracked object trước, sau đó mới chạy face detector và embedding.

## Tác động

- Không thể tạo dataset khuôn mặt tự động từ camera 2.
- Không thể ghi ID người, bbox, confidence và timestamp từ event Frigate.
- Không thể nghiệm thu face recognition.
- Không nên bật notifier thực tế dựa trên event camera 2.

## Phần không bị ảnh hưởng

- Source nguyên bản của Frigate chưa bị sửa.
- Runtime media/config/database vẫn nằm ngoài Git trên ổ `E:`.
- Hai mock publisher vẫn có thể phát stream.
- Tên hiển thị đã cấu hình: `Gate In Camera` và `Camera 2 - Face Recognition`.

## Việc cần làm tiếp

1. Kiểm tra debug view Frigate để xác nhận có bounding box `person` trên camera 2.
2. Kiểm tra output detector và post-processing ONNX bên trong Frigate.
3. Thử model COCO/OpenVINO tích hợp sẵn của Frigate làm baseline cho `person`.
4. Xác nhận gate-in có event mới theo timestamp hiện tại.
5. Chỉ sau khi có `person event` mới chạy lại collector và tạo Face Library dataset.

## Trạng thái nghiệm thu

| Hạng mục | Trạng thái |
|---|---|
| Frigate healthy | Đạt |
| Mock stream gate-in | Đạt ở mức stream |
| Mock stream camera 2 | Đạt ở mức stream |
| Gate-in event mới | Chưa đạt |
| Camera 2 person event | Chưa đạt |
| Face processor khởi tạo | Đạt |
| Face attempt/dataset | Chưa đạt |
| Tracking report có dữ liệu | Chưa đạt |
| Face identity | Chưa triển khai nghiệm thu |
