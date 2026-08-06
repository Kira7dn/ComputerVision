# PRD: MVP Camera AI An ninh tại Cổng bảo vệ

**Phiên bản:** 0.1  
**Ngày:** 05/08/2026  
**Trạng thái:** Draft

**Runtime hiện tại:** đang giới hạn ở hai mock camera: `gate_in_camera` (CCTV biển số) và `face_camera` (choke point/face). `gate_out_camera` và `safety_camera` chưa chạy trong cấu hình hiện tại.

**Cập nhật runtime Phase 4:** 05/08/2026 — chỉ dùng mock `gate_in_camera` bằng concat stream đã normalize; `gate_out_camera` và `safety_camera` tạm dừng.

## 1. Tổng quan

MVP xây dựng hệ thống camera AI phục vụ an ninh tại cổng bảo vệ. Frigate là nền tảng trung tâm để tiếp nhận stream, phát hiện đối tượng, quản lý event, snapshot, recording và cung cấp giao diện giám sát.

Các năng lực nghiệp vụ chuyên biệt như đọc biển số, nhận diện người theo thư viện ảnh, phát hiện hút thuốc, phát hiện té ngã và gửi thông báo sẽ được tích hợp thông qua một event integration service.

## 2. Mục tiêu

- Giám sát tập trung hoạt động ra vào tại cổng bảo vệ.
- Kiểm soát xe dựa trên biển số và danh sách được phép.
- Kiểm soát người dựa trên thư viện hình ảnh đã đăng ký.
- Cảnh báo các tình huống hút thuốc và té ngã.
- Lưu snapshot, clip và metadata cho từng sự kiện.
- Gửi cảnh báo qua Telegram và Zalo.
- Có thể kiểm thử toàn bộ pipeline bằng ảnh mock khi chưa có camera thật.
- Chạy local bằng Docker, không phụ thuộc cloud để phát hiện và lưu bằng chứng.

## 3. Phạm vi MVP

### 3.1 Trong phạm vi

- Một camera thật tại cổng bảo vệ.
- Một camera logic phát hiện hút thuốc.
- Một camera logic phát hiện người ngã.
- Mock camera phát ảnh thay cho camera thật trong quá trình kiểm thử.
- Phát hiện người và phương tiện bằng Frigate.
- Đọc biển số và đối chiếu whitelist.
- Nhận diện người theo danh sách hình ảnh.
- Phát hiện hút thuốc trong vùng được cấu hình.
- Phát hiện người ngã và xác nhận sau thời gian chờ.
- Lưu event, snapshot và clip trong Frigate.
- Gửi thông báo Telegram và Zalo.
- Giao diện Frigate cho live view và xem lại event.

### 3.2 Ngoài phạm vi

- Điều khiển barrier hoặc khóa cửa.
- Tích hợp ERP, HRM, WMS, MES.
- Tính ca làm việc hoặc lịch ra vào.
- Quy tắc cảnh báo theo lịch thời gian.
- Dashboard KPI doanh nghiệp.
- Nhận diện khuôn mặt từ nguồn cloud.
- Multi-site hoặc multi-tenant.
- Tự động xử lý vi phạm thay cho nhân viên bảo vệ.

## 4. Đối tượng sử dụng

| Vai trò | Nhu cầu |
|---|---|
| Bảo vệ | Xem camera, nhận và xử lý cảnh báo |
| Trưởng ca | Tra cứu event, kiểm tra clip bằng chứng |
| Quản trị viên | Cấu hình camera, whitelist, thư viện ảnh, vùng và notification |

## 5. Các nguồn camera

| Tên | Mục đích | Nguồn MVP |
|---|---|---|
| `gate_in_camera` | Kiểm soát xe và người đi vào | Mock stream |
| `gate_out_camera` | Kiểm soát xe và người đi ra | Mock stream |
| `safety_camera` | Phát hiện hút thuốc và người ngã | Mock stream |

Tên camera trong Frigate phải ổn định để metadata event, notification và audit log không bị thay đổi.

## 6. User stories

- Là bảo vệ, tôi muốn xem live camera tại cổng để biết tình trạng ra vào hiện tại.
- Là bảo vệ, tôi muốn nhận cảnh báo khi xe không có trong whitelist.
- Là bảo vệ, tôi muốn biết người ra vào có nằm trong thư viện được phép hay không.
- Là trưởng ca, tôi muốn mở snapshot và clip của event để kiểm tra lại sự việc.
- Là quản trị viên, tôi muốn thêm hoặc vô hiệu hóa biển số trong whitelist.
- Là quản trị viên, tôi muốn thêm ảnh mẫu cho một người đã đăng ký.
- Là bảo vệ, tôi muốn nhận cảnh báo khi có người hút thuốc hoặc bị ngã.
- Là người phát triển, tôi muốn gửi ảnh mock để kiểm thử mà không cần camera thật.

## 7. Yêu cầu chức năng

### FR-01. Tiếp nhận camera

- Frigate tiếp nhận RTSP stream.
- Tự động reconnect khi stream bị gián đoạn.
- Hiển thị trạng thái online, offline hoặc reconnecting.
- Không ghi password RTSP vào log ứng dụng.
- RTSP URL và credential phải được quản lý như secret local.

### FR-02. Phát hiện đối tượng

- Phát hiện tối thiểu `person` và `car`.
- Có thể bật thêm `truck`, `motorcycle` khi model hỗ trợ.
- Cho phép giới hạn detection theo zone.
- Mỗi event phải có camera, thời gian, label và confidence.

### FR-03. Kiểm soát biển số

Khi Frigate phát hiện phương tiện trong vùng cổng:

1. Tạo event và lấy snapshot phù hợp.
2. Gửi snapshot hoặc frame cho module ANPR.
3. Chuẩn hóa biển số đọc được.
4. Đối chiếu với whitelist.
5. Ghi kết quả vào event integration service.
6. Gửi cảnh báo nếu biển số không hợp lệ hoặc không đọc được.

Metadata tối thiểu:

```json
{
  "camera": "gate_camera",
  "event_id": "frigate-event-id",
  "plate": "29A12345",
  "vehicle_type": "car",
  "direction": "unknown",
  "matched": false,
  "confidence": 0.91,
  "timestamp": "2026-08-05T10:00:00+07:00",
  "snapshot_url": "...",
  "clip_url": "..."
}
```

MVP cho phép `direction=unknown`. Xác định vào/ra chỉ triển khai khi góc camera và vùng đi qua đủ ổn định.

### FR-04. Kiểm soát người theo thư viện ảnh

- Quản trị viên có thể gán một hoặc nhiều ảnh cho một người.
- Module nhận diện nhận snapshot từ event của Frigate.
- Kết quả gồm tên định danh, confidence và trạng thái matched.
- Người không khớp thư viện phải tạo event `unknown_person`.
- Ảnh không đủ chất lượng phải tạo trạng thái `unreadable`, không tự kết luận là người lạ.

Metadata tối thiểu:

```json
{
  "camera": "gate_camera",
  "event_id": "frigate-event-id",
  "person_id": "employee-001",
  "person_name": "Nguyen Van A",
  "matched": true,
  "confidence": 0.91,
  "timestamp": "2026-08-05T10:00:00+07:00",
  "snapshot_url": "..."
}
```

### FR-05. Phát hiện hút thuốc

- Camera `smoking_camera` phải có vùng phát hiện được cấu hình.
- Hệ thống phát hiện điếu thuốc, vape hoặc hành vi đưa vật thể lên miệng theo khả năng của model.
- Event chỉ được xác nhận khi đạt confidence tối thiểu.
- Event lưu snapshot và clip.
- Event gửi cảnh báo `smoking_detected`.

### FR-06. Phát hiện người ngã

- Camera `fall_camera` phát hiện thay đổi tư thế từ đứng/ngồi sang nằm.
- Event không xác nhận ngay chỉ vì một frame nằm.
- Người phải nằm liên tục trong khoảng mặc định 3–5 giây để giảm báo giả.
- Event lưu snapshot, clip trước và sau thời điểm phát hiện.
- Event gửi cảnh báo mức cao `fall_detected`.

### FR-07. Mock camera

Mock camera phải hỗ trợ:

- Gửi một ảnh đơn.
- Gửi chuỗi ảnh theo thứ tự.
- Phát ảnh lặp theo khoảng thời gian.
- Chọn kịch bản: xe hợp lệ, xe không hợp lệ, người đã đăng ký, người lạ, hút thuốc và té ngã.
- Gắn metadata test để phân biệt event mock và event camera thật.

Mock image phải đi qua stream adapter rồi vào pipeline Frigate, không gọi thẳng business logic. Nhờ đó test phản ánh gần đúng luồng vận hành thực tế.

### FR-08. Notification

Kênh MVP:

- Telegram Bot.
- Zalo OA hoặc Zalo webhook, tùy credential và hạ tầng được cung cấp.

Các event mặc định gửi cảnh báo:

- `unknown_vehicle`.
- `unreadable_plate`.
- `unknown_person`.
- `smoking_detected`.
- `fall_detected`.
- `camera_offline`.

Thông báo phải gồm loại cảnh báo, camera, thời gian, mô tả, snapshot và link event/clip nội bộ nếu có.

Notification phải có retry và cooldown. Nếu gửi thất bại, event và clip vẫn phải được giữ lại trong Frigate.

## 8. Zone đề xuất

- `gate_area`: toàn bộ khu vực cổng.
- `vehicle_lane`: làn xe đi qua.
- `person_lane`: làn người đi qua.
- `restricted_area`: vùng phía trong hoặc vùng không được phép đứng.
- `smoking_area`: vùng áp dụng luật cấm hút thuốc.
- `fall_area`: vùng cần theo dõi tư thế/ngã.

MVP bắt buộc triển khai `gate_area`, `vehicle_lane`, `person_lane`, `smoking_area` và `fall_area` nếu góc camera cho phép.

## 9. Kiến trúc hệ thống

```text
RTSP camera / Mock image
          |
   Image-to-stream adapter
          |
       Frigate
   |      |       |
go2rtc  Events  Recordings
          |
  Event integration service
   |       |        |       |
 ANPR  Face ID  Smoking  Fall
          |
   Whitelist / Face library
          |
  Notification router
      |          |
  Telegram      Zalo
```

Frigate là system of record cho camera event, snapshot, clip và trạng thái stream. Event integration service là system of record cho kết quả nghiệp vụ ANPR, face matching và trạng thái notification.

## 10. Ranh giới Frigate và integration service

### Frigate cung cấp

- RTSP ingest.
- go2rtc/restream.
- Motion và object detection.
- Zone và event.
- Recording, snapshot và clip.
- Web UI và API.
- Trạng thái camera.

### Integration service cung cấp

- ANPR.
- Face recognition theo thư viện ảnh.
- Smoking detection chuyên biệt.
- Fall detection chuyên biệt.
- Whitelist biển số.
- Face library.
- Quy tắc cảnh báo.
- Telegram/Zalo adapter.
- Retry, cooldown và notification audit.

## 11. Dữ liệu chính

### Vehicle whitelist

```json
{
  "plate": "29A12345",
  "owner_name": "Nguyen Van A",
  "vehicle_type": "car",
  "enabled": true,
  "note": "Xe nhân viên"
}
```

### Face library entry

```json
{
  "person_id": "employee-001",
  "display_name": "Nguyen Van A",
  "image_paths": ["faces/employee-001/01.jpg"],
  "enabled": true
}
```

### Event nghiệp vụ

Mỗi event phải có `id`, `event_type`, `camera`, `frigate_event_id`, `timestamp`, `confidence`, `snapshot`, `clip`, `status` và `notification_status`.

## 12. Bảo mật và riêng tư

- RTSP password, Telegram token và Zalo credential không được commit vào Git.
- Credential phải lấy từ environment hoặc file secret local.
- Đổi password Frigate mặc định ngay sau lần đăng nhập đầu tiên.
- Chỉ cho phép truy cập giao diện trong LAN hoặc qua reverse proxy có xác thực.
- Phân quyền người xem camera và quản trị viên.
- Face library phải được bảo vệ như dữ liệu nhạy cảm.
- Có khả năng xóa người, ảnh mẫu và event theo chính sách lưu trữ.
- Log không được chứa password, token hoặc dữ liệu khuôn mặt dạng thô.

## 13. Phi chức năng

- Frigate tự khởi động lại sau khi Docker restart.
- Integration service tự reconnect Frigate API.
- Event không bị mất khi Telegram/Zalo tạm thời lỗi.
- Event processing có idempotency theo `frigate_event_id`.
- Thời gian từ lúc phát hiện đến lúc tạo event mục tiêu: dưới 5 giây trong điều kiện mạng và phần cứng phù hợp.
- Notification retry tối thiểu 3 lần với backoff.
- Recording retention mặc định 2 ngày trong MVP và có thể cấu hình.
- Mock test không cần camera vật lý.

## 14. Tiêu chí nghiệm thu

### Camera và Frigate

- [x] `gate_camera` nhận stream ổn định (triển khai thực tế dùng `gate_in_camera` và `gate_out_camera`).
- [x] `smoking_camera` và `fall_camera` có thể chạy bằng RTSP hoặc mock stream (MVP dùng `safety_camera`).
- [x] Frigate hiển thị live view.
- [x] Frigate tạo event xe `car` cho `gate_in_camera` và `gate_out_camera`.
- [x] Event có snapshot và clip mở được.
- [x] Camera mất kết nối được phát hiện và tự reconnect.

### ANPR và người ra vào

- [ ] Biển số trong whitelist được đánh dấu hợp lệ.
- [ ] Biển số ngoài whitelist tạo `unknown_vehicle`.
- [ ] Người có ảnh mẫu được nhận diện khi chất lượng đủ tốt.
- [ ] Người không khớp tạo `unknown_person`.
- [ ] Ảnh không đủ chất lượng không bị kết luận sai.

### Hút thuốc và té ngã

- [ ] Mock scenario hút thuốc tạo `smoking_detected`.
- [ ] Mock scenario người ngã tạo `fall_detected`.
- [ ] Fall event chỉ xác nhận sau thời gian giữ tư thế nằm.
- [ ] Event có clip bằng chứng.

### Thông báo

- [ ] Telegram nhận được text và snapshot.
- [ ] Zalo nhận được thông báo qua adapter được cấu hình.
- [ ] Notification có cooldown, không spam event lặp.
- [ ] Notification lỗi không làm mất event Frigate.

### Vận hành

- [ ] Container tự khởi động lại.
- [ ] Không có credential trong Git hoặc log.
- [ ] Có thể chạy toàn bộ kịch bản bằng mock camera.
- [ ] Có hướng dẫn backup cấu hình, whitelist, face library và media.

## 15. Lộ trình triển khai

### P0: Nền tảng và cổng

- Chạy Frigate bằng Docker.
- Kết nối ba mock stream `gate_in_camera`, `gate_out_camera` và `safety_camera`.
- Cấu hình zone, recording và event.
- Xây event integration service cơ bản.
- Mock camera và test pipeline.
- ANPR whitelist.
- Telegram notification.

### P1: Nhận diện và cảnh báo an toàn

- Face library và face recognition.
- Zalo adapter.
- Smoking detection.
- Fall detection.
- Cooldown, retry và audit notification.

### P2: Hoàn thiện vận hành

- UI quản lý whitelist và face library.
- Báo cáo xe/người ra vào.
- Xác định hướng vào/ra.
- Phân quyền nâng cao.
- Tích hợp barrier hoặc access control.

## 16. Rủi ro và giả định

| Rủi ro | Ảnh hưởng | Hướng xử lý |
|---|---|---|
| Góc camera không đủ rõ biển số | ANPR sai hoặc không đọc được | Điều chỉnh góc, ánh sáng và vùng xe |
| Ảnh mặt quá nhỏ hoặc bị che | Face matching không ổn định | Dùng trạng thái `unreadable`, không kết luận người lạ |
| Model hút thuốc/fall báo giả | Cảnh báo sai | Zone, confidence và thời gian xác nhận |
| Zalo API chưa sẵn credential | Không gửi được Zalo | Hoàn thiện Telegram trước, giữ event trong hệ thống |
| CPU không đủ cho nhiều model | Tăng độ trễ | Giới hạn FPS, dùng detector phù hợp hoặc hardware accelerator |
| Camera mất mạng | Mất event trực tiếp | Reconnect, health status và cảnh báo offline |

Giả định MVP có quyền sử dụng hợp pháp dữ liệu khuôn mặt và có người phụ trách xác nhận các event cảnh báo.

## 20. Cập nhật nghiệm thu runtime ngày 05/08/2026

- [x] `gate_in_camera` loop 6 video trong `mock_videos/car-number-plate-video/cam-in`.
- [x] `gate_out_camera` đã được tạm tắt khỏi runtime mock.
- [x] Chuẩn hóa video trước concat về `1280x720`, `15 FPS`, `H.264`, `yuv420p`, SAR cố định.
- [x] Frigate healthy; `gate_in_camera` đạt khoảng `10 FPS` runtime.
- [x] Có event `car` qua Frigate API trên `gate_in_camera`.
- [x] Event có clip; snapshot có ở một số event đã hoàn tất.
- [x] Không có `hflip` trong command stream gate-in/gate-out.
- [x] `safety_camera` đang tạm dừng theo phạm vi hiện tại.
- [ ] Chưa nghiệm thu OCR thực tế cho `KA02MM9091` và `KA02MN1826`.
- [ ] Chưa đánh dấu notification thật đạt trong lần kiểm tra này.

## 17. Giả định kỹ thuật đã chốt cho MVP Demo

Vì mục tiêu của MVP là chứng minh tính kỹ thuật và kiến trúc, các lựa chọn dưới đây được chốt theo hướng đơn giản, dễ thay thế và đủ để trình diễn end-to-end.

1. **Phần cứng:** chạy trên GPU NVIDIA RTX 3050 thông qua Docker Desktop/WSL2 và NVIDIA Container Toolkit. CPU là fallback khi kiểm thử cấu hình, không phải cấu hình triển khai chính.
2. **ANPR:** dùng một adapter local có interface ổn định. Nếu model ANPR thật chưa sẵn sàng, adapter dùng kết quả mock có cấu trúc giống kết quả thật. Kiến trúc vẫn chứng minh được luồng Frigate event → ANPR → whitelist → notification.
3. **Face recognition:** dùng face library local đơn giản và adapter có thể thay model. MVP chỉ cần chứng minh ba trạng thái `known_person`, `unknown_person`, `unreadable`.
4. **Smoking/fall:** ưu tiên custom model chạy trực tiếp trong Frigate. Chỉ dùng inference adapter bên ngoài nếu model không tương thích với Frigate.
5. **Mock camera:** dùng FFmpeg phát ảnh thành stream giả lập để Frigate xử lý giống RTSP thật. Không tạo đường xử lý riêng bỏ qua Frigate.
6. **Lưu trữ:** dùng bind mount trên ổ E cho `/config` và `/media/frigate`, tránh phụ thuộc vị trí Docker volume mặc định.
7. **Tài nguyên:** detect ở 5 FPS, retention ngắn 1–2 ngày, một làn xe mỗi camera. Đây là đủ cho Demo và tránh phải tối ưu sớm.
8. **Event API:** dùng Frigate MQTT nếu bật được broker local; nếu không, dùng Frigate API/WebSocket. Integration service phải cô lập lớp transport để đổi cách đọc event không ảnh hưởng business logic.
9. **Notification service:** tách container riêng khỏi Frigate để chứng minh ranh giới kiến trúc. Service có Telegram adapter và Zalo adapter cùng một interface.
10. **Notification:** dùng Telegram và Zalo thật. Token, webhook URL và credential chỉ nằm trong `.env.local` ở workspace, không ghi vào Git hoặc `config.yml`. Docker Compose/container sẽ nhận credential qua `env_file` hoặc `--env-file`.

## 18. Các điểm duy nhất cần cung cấp khi triển khai thật

Đây không phải câu hỏi mở rộng phạm vi, chỉ là đầu vào không thể tự tạo:

1. Telegram Bot token và chat/group đích trong `.env.local`.
2. Zalo OA/webhook credential và nơi nhận trong `.env.local`.
3. Video mock hoặc ảnh mẫu tối thiểu cho các kịch bản xe, người, hút thuốc và té ngã.

## 19. TODO chính: Technical Validation MVP

Mục tiêu của TODO này là trước hết xác nhận các tính năng native và cách sử dụng Frigate, sau đó chứng minh kiến trúc integration hoạt động end-to-end trên RTX 3050. Không tối ưu production, không xây dashboard nghiệp vụ và không đánh giá độ chính xác AI ở mức triển khai thực tế.

### Phase 0 — Frigate Capability Discovery

- [x] Đọc config schema, tài liệu vận hành và API của đúng phiên bản Frigate đang chạy.
- [x] Lập danh sách tính năng native: camera/stream, go2rtc, motion, object detection, zone, recording, snapshot, review/event, MQTT/API/WebSocket, authentication và detector GPU.
- [x] Chạy cấu hình tối thiểu với mock stream; đã mở rộng lên ba mock stream.
- [ ] Kiểm tra label/model hiện có cho xe máy, ô tô và người.
- [ ] Kiểm tra Frigate có label hoặc model native cho `smoking`, `cigarette` hoặc `vape` hay không.
- [ ] Kiểm tra Frigate có label hoặc model native cho `fall` hoặc hành vi người ngã hay không.
- [x] Nếu không có model native, ưu tiên tích hợp custom detector/model trực tiếp vào Frigate cho smoking và fall.
- [ ] Chỉ đánh giá inference bên ngoài như fallback khi model không tương thích với detector pipeline của Frigate.
- [ ] Chạy mock scenario hút thuốc và người ngã để đo event, độ trễ, FPS và VRAM trên RTX 3050.
- [x] Xác định cách lấy event, snapshot, clip và trạng thái camera từ Frigate.
- [x] Xác định rõ từng use case: Frigate native, custom model hay integration service.
- [x] Ghi lại config mẫu, command vận hành và giới hạn đã quan sát.

**Đầu ra:** capability matrix của Frigate, kết quả thử nghiệm cho smoking/fall và quyết định kiến trúc cho từng use case MVP.

### Kết quả đọc tài liệu Frigate chính thức

| Năng lực | Kết luận kỹ thuật |
|---|---|
| Object detection | Frigate native, dùng detector và model được cấu hình |
| NVIDIA RTX 3050 | Dùng ONNX/TensorRT trong image Frigate có hậu tố `-tensorrt`; cần GPU passthrough vào container |
| `person`, `car`, `motorcycle` | Có thể xử lý bằng object model/labelmap phù hợp |
| Zone, motion, event | Frigate native |
| Recording, snapshot, review | Frigate native, có API và UI |
| Event transport | MQTT topic `frigate/events` hoặc API; event có ID để liên kết và chống xử lý trùng |
| LPR/ANPR | Frigate có LPR native; cần xác nhận model/runtime trên RTX 3050 và pipeline cho `car`/`motorcycle` |
| Face recognition | Frigate có face recognition native, chạy local; cần bật cấu hình và tạo Face Library |
| Smoking | Chưa được tài liệu Frigate xác nhận là label/use case native; phải thử model/custom detector hoặc integration |
| Fall | Chưa được tài liệu Frigate xác nhận là label/use case native; phải thử model/pose/custom detector hoặc integration |
| Telegram/Zalo | Không phải workflow notification native mục tiêu của Frigate; dùng integration/third-party adapter |

Các kết luận trên là định hướng từ tài liệu, chưa thay thế kiểm thử runtime. Phase 0 phải xác nhận bằng chính image và cấu hình sẽ dùng cho Demo.

### Kết quả runtime Phase 0 ngày 2026-08-05

- Frigate `0.17.2-3d4dd3a` chạy bằng image `stable-tensorrt`, container `healthy`.
- Ba mock stream đang chạy qua MediaMTX/FFmpeg: `gate_in_camera`, `gate_out_camera`, `safety_camera`.
- Frigate API nội bộ trả đủ thống kê ba camera; tổng input khoảng 15 FPS, detection khoảng 3.8 FPS.
- GPU RTX 3050 được Frigate sử dụng cho decode; custom ONNX fall detector vẫn chạy CPU với inference khoảng 225 ms vì CUDA Graphs không tương thích model.
- Face recognition và LPR đã được bật và model phụ trợ đã tải thành công, nhưng chưa đạt nhận diện thực tế vì chưa có Face Library và detector hiện tại là custom fall model.
- Chưa có event object hợp lệ trong kiểm thử này; API `/api/events` trả danh sách rỗng.
- Chưa đánh dấu LPR/face/smoking/fall là đạt chức năng. Cần tách detector object chuẩn khỏi custom fall hoặc xây pipeline detector phù hợp trước khi nghiệm thu các event này.

### Bổ sung kiểm thử tuần tự sau Phase 0

- Với detector OpenVINO mặc định, Frigate đã tạo event `car` và `motorcycle` trên hai mock gate stream; LPR đã tải model và ghi nhận hoạt động plate detection.
- Face Library đã đăng ký được ảnh `person_0001` qua API nội bộ. Ảnh ChokePoint 96x96 không được face recognizer nhận lại, do chất lượng/crop không đủ cho bước nhận diện; chưa coi face matching là đạt.
- Custom smoking ONNX đã load thành công trong Frigate khi chuyển cấu hình model sang `/config/model_cache/smoking/best.onnx` với label `cigarette`.
- Custom fall ONNX đã được khôi phục làm cấu hình chạy cuối cùng và load thành công tại `/config/model_cache/fall/best_640.onnx`.
- Do Frigate dùng một object model chính cho detector, object model mặc định và custom smoking/fall được kiểm thử tuần tự, không chạy đồng thời trong cùng config.

### Phase 1 — Chốt baseline và môi trường chạy

- [x] Chuẩn hóa Docker Desktop, WSL2 và GPU runtime cho RTX 3050.
- [x] Xác nhận container có thể nhìn thấy GPU bằng một bài test tối thiểu.
- [x] Chuyển toàn bộ config/media sang bind mount trên ổ E.
- [x] Tạo `.env.example` chỉ chứa tên biến Telegram/Zalo.
- [x] Kiểm tra `.env.local` nằm ngoài Git và credential không xuất hiện trong log/container image.
- [x] Ghi lại baseline: phiên bản Docker, Frigate, GPU, VRAM, CPU và RAM.

**Kết quả Phase 1 ngày 2026-08-05:** Docker Server `29.6.2`, Docker Compose `v5.3.1`, Frigate `0.17.2-3d4dd3a`, image `stable-tensorrt`, GPU `NVIDIA GeForce RTX 3050 Laptop GPU`. Frigate healthy với ba mock stream; `/config` và `/media/frigate` là bind mount từ `E:\Docker\Frigate`. Runtime API ghi nhận khoảng 15 FPS input, custom fall ONNX khoảng 170 ms/inference, GPU được container nhận diện và `nvidia-smi` hoạt động.

**Lệnh vận hành:** dùng duy nhất `powershell -ExecutionPolicy Bypass -File .\deploy\run.ps1 <command>`. Chạy command `help` để xem `start/status/logs/doctor/stop` và các tác vụ bảo trì. Compose reference nằm tại `deploy/reference/docker-compose.yml`.

### Quản lý source và runtime

- Repo Git duy nhất của workspace là `D:\BusinessAnalyze\Camera`, branch chính `main`.
- Thư mục `frigate/` là source clone local dùng để đọc tài liệu và kiểm tra kỹ thuật; thư mục này bị ignore trong repo Camera.
- Metadata Git riêng của Frigate đã được vô hiệu hóa, vì vậy Frigate không còn branch hoặc remote độc lập trong workspace.
- `mock_videos/` cũng bị ignore vì chỉ là dữ liệu runtime/local test.
- Runtime Frigate được mount từ `E:\Docker\Frigate\config` và `E:\Docker\Frigate\media`; source/config mẫu và script vận hành vẫn nằm trong workspace Camera.

**Đầu ra:** môi trường chạy lặp lại được, có bằng chứng GPU và không lộ secret.

### Phase 2 — Frigate core với ba mock stream

- [ ] Chuẩn bị bộ video mock có license phù hợp cho xe máy, ô tô, người, hút thuốc và té ngã.
- [x] Chuẩn hóa video về format Frigate dùng ổn định: H.264, 1280×720, 15 FPS, CFR, GOP 30.
- [x] Tạo mock stream cho đúng ba camera: `gate_in_camera`, `gate_out_camera`, `safety_camera`.
- [x] Chạy đồng thời ba stream trong Frigate.
- [x] Xác nhận live view, reconnect và trạng thái offline/online.
- [x] Cấu hình zone và label tối thiểu: `person`, `car`, `motorcycle`.

**Đầu ra:** Frigate nhận và xử lý ba stream cùng lúc, không phụ thuộc camera thật.

### Phase 3 — Xác nhận detector và GPU performance

- [x] Xác nhận detector thực sự dùng RTX 3050, không âm thầm fallback CPU.
- [x] Đo FPS xử lý, độ trễ event, GPU utilization, VRAM, CPU và RAM khi chạy ba stream.
- [ ] Xác nhận Frigate tạo event cho người, ô tô và xe máy.
- [ ] Xác nhận zone chỉ tạo event trong khu vực cấu hình.
- [x] Xác định cấu hình detect tối thiểu đủ ổn định cho Demo (`detect.fps: 10`).

**Đầu ra:** bảng đo hiệu năng và kết luận cấu hình GPU/detect dùng cho Demo.

### Phase 4 — Recording, snapshot và event contract

Phase 4 chuẩn hóa event ô tô theo contract sau (Frigate vẫn là nguồn event/media):

```json
{
  "event_id": "frigate-event-id",
  "camera": "gate_in_camera",
  "direction": "in",
  "vehicle_type": "car",
  "plate": "KA02MM9091",
  "plate_confidence": 0.91,
  "plate_status": "recognized",
  "started_at": "2026-08-05T10:00:00Z",
  "ended_at": "2026-08-05T10:00:05Z",
  "snapshot_url": "http://frigate:5000/api/events/<event_id>/snapshot.jpg",
  "clip_url": "http://frigate:5000/api/events/<event_id>/clip.mp4",
  "plate_result": "allowed"
}
```

Chỉ `car` tại `gate_in_camera`/`gate_out_camera` được chuẩn hóa; hướng lần lượt là `in`/`out`. Không có `recognized_license_plate` thì giữ `plate: null` và `plate_status: unreadable`, không đoán biển số. Whitelist là JSON ngoài Git, gồm `plate`, `owner_name`, `vehicle_type`, `enabled` và `note`; kết quả là `allowed`, `not_allowed` hoặc `unreadable`.

Notifier chỉ xử lý event đã có `end_time`, gửi snapshot thật qua Telegram, truyền contract kèm URL clip cho Zalo, retry tối đa cấu hình được và lưu trạng thái từng kênh theo `event_id` trong runtime trên ổ E. Credential chỉ lấy từ `.env.local`; lỗi notification không xóa event/media Frigate.

- [x] Xác nhận mỗi event có snapshot và clip.
- [x] Xác nhận media được ghi vào ổ E và đọc lại được sau khi restart container.
- [x] Xác nhận event có `camera`, `event_id`, `label`, `confidence`, timestamp và zone.
- [x] Chọn Frigate API làm transport hiện tại cho integration service.
- [x] Xây adapter đọc event và idempotency theo `frigate_event_id`.
- [ ] Kiểm thử camera restart, container restart và event lặp.

**Đầu ra:** event contract ổn định và bằng chứng media không bị mất.

### Phase 5 — Integration contract cho nghiệp vụ

- [ ] Xây interface ANPR nhận snapshot và trả biển số, loại xe, confidence và trạng thái đọc.
- [ ] Xây interface face nhận snapshot và trả `known_person`, `unknown_person` hoặc `unreadable`.
- [ ] Tích hợp custom smoking model vào Frigate và chuẩn hóa label/event `smoking_detected`.
- [ ] Tích hợp custom fall model vào Frigate và chuẩn hóa label/event `fall_detected`.
- [ ] Nếu fall model cần pose/temporal inference không được Frigate hỗ trợ trực tiếp, ghi nhận blocker và tạo fallback adapter tối thiểu.
- [ ] Dùng mock model chỉ để kiểm tra contract khi model thật chưa sẵn sàng.
- [ ] Xác nhận business event luôn giữ liên kết tới Frigate event, snapshot và clip.

**Đầu ra:** có thể thay model thật mà không thay đổi Frigate pipeline hoặc notification contract.

### Phase 6 — Notification thật

- [ ] Load credential Telegram/Zalo từ `.env.local` vào integration container.
- [ ] Gửi Telegram text + snapshot cho ít nhất một event.
- [ ] Gửi Zalo text + snapshot cho ít nhất một event.
- [ ] Thêm retry có giới hạn, cooldown và chống gửi trùng.
- [ ] Kiểm thử notification lỗi nhưng Frigate event/clip vẫn được giữ.
- [ ] Kiểm tra token không xuất hiện trong log, exception hoặc payload không liên quan.

**Đầu ra:** chứng minh được đường đi event → notification thật.

### Phase 7 — Demo acceptance

- [ ] Chạy một kịch bản xe máy vào và đối chiếu whitelist.
- [ ] Chạy một kịch bản ô tô ra và đối chiếu whitelist.
- [ ] Chạy một kịch bản người có trong danh sách.
- [ ] Chạy một kịch bản người không có trong danh sách.
- [ ] Chạy một kịch bản hút thuốc.
- [ ] Chạy một kịch bản người ngã.
- [ ] Với mỗi kịch bản, kiểm tra đủ: Frigate event, snapshot, clip, integration result và Telegram/Zalo notification.
- [ ] Ghi lại thời gian xử lý, tài nguyên GPU và lỗi phát sinh.
- [ ] Đóng gói lệnh khởi động và hướng dẫn chạy lại Demo từ đầu.

**Tiêu chí kết thúc:** pipeline ba mock stream chạy đồng thời trên RTX 3050 và hoàn thành được ít nhất một luồng end-to-end cho mỗi năng lực nghiệp vụ mà không cần camera vật lý.

## 20. Quyết định cố định cho MVP Demo

### 18.1 Số lượng và vai trò camera

MVP Demo sử dụng đúng 3 mock camera, không phụ thuộc camera RTSP hoặc thiết bị vật lý:

| Camera | Vai trò |
|---|---|
| `gate_in_camera` | Kiểm soát xe và người đi vào |
| `gate_out_camera` | Kiểm soát xe và người đi ra |
| `safety_camera` | Phát hiện hút thuốc và người ngã |

Không triển khai lịch làm việc, ca làm việc hoặc rule theo thời gian trong MVP Demo.

### 18.2 Cấu hình mock camera cổng

Để mô phỏng việc đọc ô tô trong bản Demo, mock stream phải phát ảnh/chuỗi ảnh có đủ độ chi tiết biển số. Cấu hình mô phỏng mục tiêu là:

- Độ phân giải: **1920×1080 tối thiểu**.
- FPS: **15 FPS**.
- Codec: H.264.
- Shutter mục tiêu: khoảng **1/500 giây hoặc nhanh hơn** khi có thể cấu hình.
- Hỗ trợ hồng ngoại hoặc ánh sáng đủ ổn định cho ban đêm.
- Camera đặt đối diện làn xe, hạn chế góc xiên.
- Góc lệch ngang so với hướng biển số: tối đa khoảng **25–30 độ**.
- Góc lệch dọc: tối đa khoảng **15 độ**.
- Khoảng cách vùng đọc biển số: khoảng **5–8 m**.
- Chiều rộng biển số trong ảnh mục tiêu: tối thiểu khoảng **100–130 pixel**.
- Mỗi camera chỉ nên quan sát một làn xe trong MVP Demo.

Đây là cấu hình cho mock stream, không phải yêu cầu phần cứng camera thật. MVP không kiểm tra góc lắp đặt, ánh sáng hoặc kết nối RTSP vật lý.

### 18.3 Cách xử lý ô tô

- Frigate chỉ theo dõi `person` và `car` trong phạm vi MVP Demo.
- ANPR chỉ nhận snapshot/frame của ô tô.
- Whitelist chỉ là danh sách đơn giản gồm biển số, loại xe, tên chủ xe và trạng thái bật/tắt.
- Không triển khai database nghiệp vụ phức tạp hoặc tích hợp ERP trong Demo.
- Khi không đọc được biển số, hệ thống lưu event `unreadable_plate` để bảo vệ kiểm tra thủ công.

### 18.4 Danh sách người

MVP Demo chỉ dùng một danh sách hình ảnh đơn giản:

- Mỗi người có `person_id`, tên hiển thị, ảnh mẫu và trạng thái bật/tắt.
- Không có lịch hiệu lực, ca làm việc hoặc phân quyền nghiệp vụ phức tạp.
- Kết quả gồm `known_person`, `unknown_person` hoặc `unreadable`.

### 18.5 Camera an toàn

`safety_camera` dùng chung cho hai kịch bản:

- Hút thuốc trong vùng cấm.
- Người ngã trong vùng quan sát.

Để giảm báo giả trong Demo:

- Chỉ cấu hình một vùng an toàn rõ ràng.
- Event té ngã cần giữ trạng thái nằm khoảng 3–5 giây.
- Event hút thuốc dùng confidence và cooldown đơn giản.

### 18.6 Mock camera

Mock camera mô phỏng cả 3 vai trò camera bằng ảnh hoặc chuỗi ảnh:

- Mock xe vào.
- Mock xe ra.
- Mock người đã có trong danh sách.
- Mock người không có trong danh sách.
- Mock hút thuốc.
- Mock người ngã.

Mock vẫn đi qua pipeline Frigate để kiểm tra event, snapshot, clip và notification thống nhất với camera thật.

### 18.7 Tiêu chí thành công Demo

Demo được xem là đạt khi trình diễn được trọn vẹn các luồng sau:

1. Ô tô đi vào camera `gate_in_camera`, đọc biển số và đối chiếu danh sách.
2. Ô tô đi ra camera `gate_out_camera`, đọc biển số và đối chiếu danh sách.
3. Người có ảnh trong danh sách được nhận diện.
4. Người không có trong danh sách tạo cảnh báo.
5. Mock hoặc `safety_camera` tạo event hút thuốc.
6. Mock hoặc `safety_camera` tạo event người ngã.
7. Mỗi event có snapshot/clip và gửi được Telegram/Zalo.

### Phase 2 runtime result

Ngày kiểm thử: 2026-08-05.

- Đã thêm profile `deploy/config.phase2-native.yml` dùng model object native của Frigate.
- Đã thêm `deploy/start-phase2.ps1`; profile này bật zone, snapshot, recording và ba mock stream.
- Ba stream chạy đồng thời qua MediaMTX: `gate_in_camera`, `gate_out_camera`, `safety_camera`.
- `/api/stats` ghi nhận khoảng 5 FPS mỗi camera; tổng input khoảng 15 FPS.
- Mock gate tạo được event `car` và `motorcycle` trên `/api/events`.
- Snapshot/recording được tạo trên `E:\Docker\Frigate\media`.
- Đã dừng publisher `gate-out` để kiểm tra mất stream, sau đó chạy lại profile; publisher và camera đã phục hồi, cả ba camera tiếp tục có FPS.
- Zone hiện dùng toàn khung hình để chứng minh wiring/configuration; chưa phải vùng làn xe thực tế.
- Log có cảnh báo timestamp/audio do video mock lặp lại; không chặn pipeline video nhưng cần chuẩn hóa mock source nếu dùng để đo độ ổn định dài hạn.

### Chuẩn hóa mock stream

- Publisher mock hiện bỏ audio và mã hóa lại toàn bộ stream thành H.264 1280×720@15 FPS.
- Timestamp được tạo lại bằng `genpts`, GOP cố định 30 frame và CFR.
- Đã kiểm tra RTSP `gate-in`: H.264, 1280×720, 15 FPS.
- Sau khi chạy lại, không còn log `Non-monotonic DTS`, `Queue input is backward in time`, mất frame hoặc `404 Not Found` trong cửa sổ kiểm tra.

### Phase 3 runtime result

Ngày kiểm thử: 2026-08-05.

- Đã export `server/yolov8n.pt` thành ONNX FP32 và chuyển detector chính sang `onnx` trong image `stable-tensorrt`.
- Frigate load model `/models/yolov8n.onnx`, detector inference khoảng 12–13 ms và RTX 3050 sử dụng thật khoảng 21% ở 5 FPS/camera.
- Khi nâng lên 15 FPS/camera, GPU khoảng 53% nhưng process thực tế chỉ khoảng 4 FPS ở hai camera cổng; phần lớn frame bị skip.
- Khi đặt `detect.fps: 10`, process đạt khoảng 8.1–8.4 FPS/camera, skipped khoảng 1.6–1.9 FPS/camera và GPU khoảng 52%.
- Cấu hình đề xuất cho MVP/production demo: input mock 15 FPS, Frigate detect 10 FPS/camera.
- Script benchmark có thể chạy lại bằng `deploy/measure-phase3.ps1`; báo cáo runtime nằm trong `.tmp/phase3/` và không đưa vào Git.
