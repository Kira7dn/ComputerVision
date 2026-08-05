# Guideline: Kiểm chứng và tích hợp Frigate cho MVP

**Runtime hiện tại:** chỉ test hai mock camera, không chạy gate-out hoặc safety.

- `gate_in_camera`: `mock_videos/car-number-plate-video/cam-in/Traffic Control CCTV.mp4` qua `/gate-in`.
- `face_camera`: `mock_videos/face-recognition/segments/01_P1E_S1_C1.mp4` qua `/chokepoint-face`.

Các ghi chú lịch sử về ba stream bên dưới không mô tả runtime hiện tại.

## 1. Capability matrix

| Năng lực | Kết luận từ tài liệu | Phải xác nhận runtime |
|---|---|---|
| Stream, go2rtc, motion, zone | Frigate native | Có |
| Object detection | Frigate native | Có |
| Recording, snapshot, review | Frigate native | Có |
| MQTT/API events | Frigate native | Có |
| `car`, `motorcycle`, `person` | Model object detection | Có |
| LPR/ANPR | Frigate native | Model, xe máy và ô tô |
| Face recognition | Frigate native, local | Face Library và GPU |
| Smoking | Không có use case native được xác nhận | Custom model trong Frigate |
| Fall | Hành vi cần model riêng | Custom model trước, fallback temporal sau |
| Telegram/Zalo | Integration/third-party | Gửi thật |

Tài liệu tham khảo: [Object Detectors](https://docs.frigate.video/configuration/object_detectors/), [LPR](https://docs.frigate.video/configuration/license_plate_recognition/), [Face Recognition](https://docs.frigate.video/configuration/face_recognition/), [MQTT](https://docs.frigate.video/integrations/mqtt/), [Snapshots](https://docs.frigate.video/configuration/snapshots/).

## 2. Nguyên tắc kiến trúc

```text
3 mock streams -> Frigate + TensorRT -> events/snapshot/clip
                                      -> MQTT/API
                                      -> integration service
                                         -> whitelist
                                         -> cooldown/idempotency
                                         -> Telegram/Zalo
```

Frigate chịu trách nhiệm stream, detector, custom model, zone, event và media. Integration service chỉ xử lý whitelist, chuẩn hóa kết quả và notification. Smoking/fall phải ưu tiên chạy model trong Frigate; service ngoài chỉ là fallback khi Frigate không tương thích model.

## 3. Phase 0: Discovery trước khi triển khai

1. Ghi lại Frigate image tag/digest, Docker, WSL2, driver và GPU.
2. Đọc config schema, detector docs, LPR, face, MQTT, API, snapshots và recording.
3. Lập capability matrix với trạng thái `documented`, `runtime_verified`, `requires_model`, `requires_integration`.
4. Xác định config và endpoint lấy event, snapshot, clip và camera status.
5. Kiểm tra riêng label/model cho `person`, `car`, `motorcycle`, `smoking`, `cigarette`, `vape`, `fall`, `fallen_person`.
6. Xác định custom model cần ONNX/TensorRT, input size, tensor layout, pixel format, labelmap và post-processing gì.
7. Chạy mock scenario smoking/fall để đo khả năng tạo event.

**Đầu ra:** capability matrix, config mẫu, API/event contract và kết luận từng use case chạy native hay cần fallback.

## 4. RTX 3050 và TensorRT

NVIDIA cần image Frigate có hậu tố `-tensorrt`, không dùng image CPU để kết luận GPU.

```powershell
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
docker pull ghcr.io/blakeblackshear/frigate:stable-tensorrt
```

Phải xác nhận:

- Container nhìn thấy RTX 3050.
- Log Frigate load ONNX/TensorRT provider.
- `nvidia-smi` thấy process dùng GPU.
- VRAM tăng khi detector chạy.
- Detector không âm thầm fallback CPU.

Ghi lại GPU utilization, VRAM, CPU, RAM, detection FPS và latency.

## 5. Ba mock stream

Tên cố định:

```text
gate_in_camera
gate_out_camera
safety_camera
```

Video mock cần H.264, 1920×1080, 15 FPS hoặc cấu hình tương đương, loop được và có license rõ ràng. Kiểm tra bằng:

```powershell
ffprobe -v error -select_streams v:0 `
  -show_entries stream=codec_name,width,height,r_frame_rate,pix_fmt `
  -of json .\mock\sample.mp4
```

Luồng bắt buộc:

```text
MP4/ảnh -> FFmpeg image/video-to-stream -> mock RTSP -> Frigate
```

Không gọi integration service trực tiếp từ script mock vì sẽ bỏ qua pipeline Frigate.

## 6. Object detection, zone và media

Bật tối thiểu các label:

```text
person
car
motorcycle
```

Kiểm tra từng camera:

1. Stream online và live view hoạt động.
2. Object trong zone tạo event.
3. Object ngoài zone không tạo business alert.
4. Event có camera, label, score, timestamp, zone và ID.
5. Snapshot và clip tạo được.
6. Restart container không mất config/media.

## 7. Vehicle và LPR

Vehicle detection là bước đầu: chạy riêng mock xe máy và ô tô, xác nhận Frigate tạo object đúng label.

LPR native của Frigate mặc định cần phát hiện `car` hoặc `motorcycle` trước. Phải test:

- LPR model được load.
- Model chạy GPU hay CPU.
- Xe máy và ô tô đều có kết quả.
- Event có plate/recognized plate.
- Kết quả xuất hiện qua MQTT/API.

Whitelist nằm ngoài Frigate:

```json
{
  "frigate_event_id": "...",
  "camera": "gate_in_camera",
  "vehicle_type": "motorcycle",
  "plate": "29A12345",
  "matched": false
}
```

## 8. Face recognition

Face recognition native của Frigate cần bật và dùng Face Library:

```yaml
face_recognition:
  enabled: true
```

Flow kiểm thử:

```text
person -> face detection -> embedding -> known sub_label/unknown
```

Test tối thiểu:

1. Thêm một người và ảnh mẫu.
2. Phát mock video người đó.
3. Xác nhận tên/sub-label.
4. Phát người khác và xác nhận unknown.
5. Phát ảnh mờ/không thấy mặt và xác nhận unreadable/không kết luận sai.

Model face `large` cần GPU/NPU phù hợp; ghi VRAM và latency trên RTX 3050.

## 9. Custom smoking model

Ưu tiên custom object model chạy trực tiếp trong Frigate. Label có thể là:

```text
smoking_person
cigarette
vape
```

Trước khi tích hợp phải có:

- Model ONNX/TensorRT và license.
- Input width/height.
- Tensor layout, pixel format, data type.
- Labelmap và post-processing.
- Model chạy được trên RTX 3050.

Quy trình:

1. Bind mount model cache vào `/config`.
2. Khai báo detector/model theo schema Frigate.
3. Khai báo label cần track.
4. Kiểm tra log load model.
5. Chạy mock smoking video.
6. Kiểm tra bounding box, label, event MQTT/API và snapshot.
7. Đo FPS, VRAM, latency và false positive.

Nếu model chỉ nhận diện điếu thuốc, không gọi kết quả là hành vi hút thuốc khi chưa có logic xác nhận tương ứng.

## 10. Custom fall model

Phải phân biệt:

- Object detector: `fallen_person` trong một frame.
- Pose model: tư thế cơ thể.
- Temporal/action model: chuỗi đứng → ngã → nằm.

Ưu tiên:

1. Chạy custom object detector `fallen_person` trong Frigate.
2. Thêm zone và yêu cầu trạng thái duy trì 3–5 giây.
3. Nếu model cần pose/temporal mà detector Frigate không biểu diễn được, ghi rõ blocker và mới dùng fallback adapter.

Event fall đạt khi có người trong `safety_zone`, label hợp lệ, trạng thái giữ đủ thời gian, snapshot/clip và một event ID duy nhất.

## 11. MQTT/API và idempotency

Topic chính:

```text
frigate/events
```

Xử lý các message `new`, `update`, `end`. Một tracked object có thể phát nhiều update cùng ID; không gửi notification cho mọi update.

Khóa idempotency:

```text
frigate_event_id + business_event_type
```

Snapshot lấy qua API event snapshot hoặc MQTT snapshot. Clip phải được truy xuất từ event ID, không tự đoán đường dẫn file.

## 12. Telegram/Zalo

Credential chỉ nằm trong `.env.local` và truyền vào integration container bằng `env_file`/`--env-file`:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ZALO_ACCESS_TOKEN=...
ZALO_DESTINATION_ID=...
```

Test flow:

```text
Frigate event -> normalize -> cooldown/idempotency -> Telegram/Zalo
```

Phải gửi được text + snapshot. Khi gửi lỗi, event/clip vẫn giữ nguyên; retry có giới hạn và log không chứa token.

## 13. Đo hiệu năng và tiêu chí đạt

Đo theo ba mức:

1. Một stream với object model.
2. Ba stream với object model.
3. Ba stream thêm lần lượt LPR, face, smoking và fall.

Ghi GPU utilization, VRAM, CPU, RAM, input FPS, detection FPS, event latency, notification latency và lỗi.

Technical validation đạt khi:

- Frigate TensorRT dùng thật RTX 3050.
- Ba mock stream chạy đồng thời.
- Person/car/motorcycle tạo event.
- LPR chạy được ít nhất một xe máy và một ô tô mock.
- Face Library nhận diện được một người mock.
- Smoking custom model chạy trong Frigate hoặc có blocker rõ ràng.
- Fall custom model chạy trong Frigate hoặc có blocker rõ ràng.
- Snapshot/clip/event ID lấy được qua MQTT/API.
- Telegram/Zalo nhận được cảnh báo thật.
- Có bảng đo hiệu năng và lệnh khởi động lại Demo.

## 14. Quy tắc kết luận

- Có trong docs không đồng nghĩa đã chạy runtime.
- Container healthy không đồng nghĩa detector dùng GPU.
- Có event person không đồng nghĩa đã xác nhận face/fall/smoking.
- Model load thành công không đồng nghĩa model trả label đúng.
- Notification gửi được không đồng nghĩa retry/cooldown đã đạt.
- Mọi kết luận cuối cùng phải kèm command, log hoặc output runtime.

## 15. Phase 4 runtime contract và kiểm thử

Notifier dùng `deploy/notifier/frigate_notifier.py`. API media của event là:

- `GET /api/events/<event_id>/snapshot.jpg`
- `GET /api/events/<event_id>/clip.mp4`

Whitelist đặt tại `E:\Docker\Frigate\runtime\vehicle_whitelist.json`, state notification tại `E:\Docker\Frigate\runtime\notifier_state.json`; cả hai được mount vào container và không nằm trong Git. File mẫu là `deploy/notifier/vehicle_whitelist.example.json`. Contract có `event_id`, camera, direction, car, plate/status/confidence, thời gian, snapshot/clip URL và `plate_result`.

Đã xác nhận ở mức contract/unit smoke: suy ra `in`/`out`, nhận đúng `recognized_license_plate`, phân loại `allowed`/`not_allowed`/`unreadable`, URL media theo event ID, credential rỗng không làm notifier crash và state có thể lưu bền qua restart. Runtime ngày 05/08/2026 đã xác nhận hai camera cổng có event `car` qua Frigate API, có clip và một số event có snapshot. OCR của `KA02MM9091`/`KA02MN1826` và notification thật vẫn chưa được đánh dấu đạt trong lần kiểm tra này.

### 15.1 Mock stream runtime

- `gate_in_camera`: 6 video được normalize trước khi concat và loop liên tục; đây là mock stream duy nhất đang bật.
- `face_camera` (camera số 2): dùng `face-recognition\segments\01_P1E_S1_C1.mp4`, track `person` qua stream `chokepoint-face`.
- `gate_out_camera`: đã bỏ khỏi cấu hình runtime.
- Chuẩn đầu ra: `1280x720`, `15 FPS`, H.264, `yuv420p`, SAR cố định.
- Frigate runtime: healthy; hai camera cổng đạt khoảng `10 FPS`.
- Kiểm tra mirror: command ffmpeg của `gate-in` không chứa `hflip`.
- `safety_camera`: cấu hình còn tồn tại nhưng publisher đang tạm dừng, nên FPS/event bằng 0.
- Runtime hiện xác nhận `gate_in_camera` khoảng 10 FPS và `face_camera` khoảng 10 FPS; Frigate healthy.
- Runtime artifact normalize và concat list nằm trong `.tmp/phase2/`, không đưa vào Git.

## 16. Quy ước source và dữ liệu local

- Workspace chỉ có một Git repository: `D:\BusinessAnalyze\Camera`, branch `main`.
- Source Frigate tại `frigate/` chỉ là bản clone local phục vụ nghiên cứu/đọc tài liệu và đã được ignore khỏi repo Camera.
- `frigate/` không còn Git metadata, remote hoặc branch riêng; không thực hiện publish source Frigate lên GitHub.

### 15.2 Theo dõi source Frigate

- Source local tại `frigate/` đã được khôi phục Git metadata chuẩn.
- Remote `upstream`: `https://github.com/blakeblackshear/frigate.git`.
- Fork `origin`: `https://github.com/Kira7dn/camera-frigate.git`.
- Branch local: `camera-base`, đang track `upstream/dev`.
- Mốc hiện tại: commit `33c00a27e4bac8b8d276a6bf6f004570bedd3b5c`.
- Branch `camera-base` đã được push lên fork; chưa có custom patch.
- Runtime vẫn dùng image chính thức `ghcr.io/blakeblackshear/frigate:stable-tensorrt`; chưa chuyển sang image build từ fork.
- Dữ liệu video tại `mock_videos/` bị ignore vì là dữ liệu mock local, không đưa vào commit.
- Dữ liệu runtime persistent nằm trên ổ E tại `E:\Docker\Frigate\config` và `E:\Docker\Frigate\media`.

## 17. Phase 2–3 runtime result

Ngày kiểm thử: 2026-08-05.

- Model `server/yolov8n.pt` đã được export thành ONNX FP32; Frigate dùng detector `onnx` trên image `stable-tensorrt`.
- Runtime xác nhận detector `onnx`, inference khoảng 12–13 ms; GPU RTX 3050 đạt khoảng 21% ở mức 5 FPS/camera.
- Benchmark 15 FPS/camera cho thấy GPU khoảng 53% nhưng gate chỉ process khoảng 4 FPS và skip khoảng 11 FPS/camera.
- Benchmark 10 FPS/camera cho thấy gate process khoảng 8.1–8.4 FPS, safety khoảng 8.3 FPS, skip khoảng 1.6–1.9 FPS/camera và GPU khoảng 52%.
- Mức vận hành đề xuất: stream input 15 FPS, `detect.fps: 10` cho cả ba camera.
- Script benchmark: `deploy/measure-phase3.ps1`; output local tại `.tmp/phase3/`.

### Kết luận Phase 3

Đạt xác nhận detector GPU, runtime ba stream và benchmark FPS/GPU cơ bản. Chưa coi là benchmark production dài hạn; vẫn cần đo 10–30 phút và hoàn thiện LPR/face/smoking/fall/notification ở các phase nghiệp vụ sau.
