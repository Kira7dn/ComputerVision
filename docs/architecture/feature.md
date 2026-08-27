# Tính năng camera trước từ openpilot

## Phạm vi

Tài liệu này tổng hợp các tính năng liên quan tới camera nhìn đường phía trước của
openpilot và mức độ có thể sử dụng độc lập trong LS-Vision. Các output perception
dùng chung một phiên inference `driving_supercombo`; không tạo một model session
riêng cho từng tính năng.

Model openpilot đã đối chiếu có SHA-256
`659727c4d4839adc4992a254409a54259a8756a743f2d567bf5fdc6579f8009b`, trùng với
model đang được pin bởi bài kiểm tra front-camera offline của LS-Vision.

## Bảng tính năng

Quy ước: `✅ Done` đã hoàn tất trong LS-Vision; `🚧 Doing` đã có một phần pipeline
hoặc contract; `⬜ Not yet` chưa triển khai trong LS-Vision.

### 1. Camera-only

Các tính năng trong nhóm này có thể chạy từ luồng camera, model và calibration
được provision trước, không bắt buộc kết nối CAN hoặc cảm biến xe ở runtime.

| Status | Tính năng | Mức độc lập | Trạng thái LS-Vision hiện tại |
| --- | --- | --- | --- |
| 🚧 Doing | Nhận diện 4 vạch làn và độ tin cậy | Cao, chỉ cần inference và calibration | Đã parse cả 4 vạch; đang chỉ render 2 vạch làn chính |
| ✅ Done | Dự đoán quỹ đạo xe phía trước | Cao cho hiển thị và telemetry | Đã render hành lang và đường tâm dự đoán |
| ✅ Done | Cảnh báo lệch làn trái/phải (LDW) | Khá độc lập; bản openpilot đầy đủ còn dùng tốc độ xe, xi-nhan và trạng thái lateral control | Đã có policy camera-only `vision_ldw_left` và `vision_ldw_right` |
| ✅ Done | Cảnh báo va chạm phía trước từ model (FCW) | Khá độc lập, nhánh `hardBrakePredicted` không bắt buộc radar | Đã có policy camera-only `vision_fcw` |
| 🚧 Doing | Nhận biết tối đa 3 xe dẫn đầu | Cao cho perception và overlay; điều khiển bám xe không độc lập | Đã parse cả 3 lead; summary mới công bố lead đầu tiên, chưa có overlay hoặc cảnh báo khoảng cách |
| 🚧 Doing | Nhận biết 2 biên đường | Cao cho hiển thị và phân tích | Đã parse nhưng chưa render và chưa có cảnh báo lệch khỏi biên đường |
| 🚧 Doing | Visual odometry: chuyển động tịnh tiến và quay của camera | Có thể xuất telemetry độc lập | Model đã trả và adapter đã parse `pose`, nhưng chưa đưa vào `FrontPerception` |
| 🚧 Doing | Ước lượng camera mounting và road transform | Có thể dùng để chẩn đoán camera bị xê dịch; tự calibration đầy đủ còn cần vận tốc xe | Đã parse `wide_from_device_euler` và `road_transform`, nhưng chưa công bố trong contract |
| 🚧 Doing | Vị trí, vận tốc, gia tốc, hướng và tốc độ quay dự đoán trong 10 giây | Độc lập cho telemetry; không độc lập cho điều khiển xe | Hiện chỉ giữ ba thành phần position của `plan` |
| 🚧 Doing | Confidence, disengagement và dự đoán hành vi ga/phanh | Có thể dùng cho diagnostics | Hiện chỉ dùng xác suất hard-brake 3 m/s² và 5 m/s² |
| ✅ Done | Ghi hình, snapshot, thumbnail và livestream | Độc lập về media | Openpilot có sẵn, nhưng LS-Vision và MediaMTX đã sở hữu các chức năng tương đương; không thêm owner thứ hai |

LDW camera-only của LS-Vision là advisory dựa trên model. So với openpilot đầy
đủ, policy này chưa dùng tốc độ xe, trạng thái xi-nhan hoặc trạng thái lateral
control. Tương tự, camera mounting và road transform có thể xuất trực tiếp từ
model, nhưng quy trình tự calibration đầy đủ cần thêm vận tốc xe.

### 2. Camera + CAN/Sensor

Các tính năng trong nhóm này dùng output camera nhưng không thể vận hành an toàn
chỉ từ video. Chúng cần trạng thái xe, CAN, cảm biến hoặc vehicle integration.

| Status | Tính năng | Dependency bổ sung | Trạng thái LS-Vision hiện tại |
| --- | --- | --- | --- |
| ⬜ Not yet | Automated Lane Centering (ALC) | Tốc độ, trạng thái lái, vehicle model, CAN controller và safety layer | Chưa thuộc camera-only runtime |
| ⬜ Not yet | Lane change assist | Xi-nhan, tương tác người lái, lateral control và vehicle integration | Chưa triển khai |
| ⬜ Not yet | Adaptive Cruise Control (ACC) | `carState`, cruise state, radar/vision fusion, longitudinal planner và CAN safety | Chưa triển khai |

### 3. Camera + CAN | Sensor | LiDAR

Nhóm này dành cho sensor-fusion mở rộng: camera kết hợp CAN và một hoặc nhiều
cảm biến như IMU, radar hoặc LiDAR. Openpilot upstream có camera, CAN, IMU và
radar fusion nhưng không cung cấp pipeline LiDAR; các mục LiDAR dưới đây là định
hướng mở rộng của LS-Vision, không phải tính năng sẵn có từ openpilot.

| Status | Tính năng | Dependency bổ sung | Trạng thái LS-Vision hiện tại |
| --- | --- | --- | --- |
| ⬜ Not yet | Hợp nhất đối tượng 3D camera–LiDAR | LiDAR point cloud, timestamp synchronization, camera–LiDAR extrinsics và 3D association | Chưa có LiDAR ingest hoặc fusion contract |
| ⬜ Not yet | Lead distance và Time-To-Collision đa cảm biến | Camera lead, CAN vehicle speed, radar hoặc LiDAR range/range-rate | Chưa có synchronized vehicle/sensor state cho `camera_front` |
| ⬜ Not yet | Cảnh báo vật cản và vùng có thể di chuyển | Camera semantics, LiDAR geometry, ego pose và occupancy representation | Chưa có occupancy hoặc free-space fusion pipeline |
| ⬜ Not yet | Automatic Emergency Braking (AEB) đa cảm biến | Sensor fusion, braking envelope, CAN actuation, vehicle safety controller và fail-safe runtime | Không thuộc baseline openpilot và chưa được phép điều khiển xe trong LS-Vision |
| ⬜ Not yet | Online sensor extrinsic calibration và drift monitoring | Camera, IMU/odometry, CAN speed, LiDAR/radar correspondences và calibration persistence | Hiện chỉ có calibration camera được provision tĩnh |

## Feature độc lập có thể bổ sung

Các feature sau nên là consumer của cùng `FrontPerception`, không mở thêm phiên
inference:

1. `front_lead_overlay`: hiển thị xác suất, khoảng cách và quỹ đạo lead.
2. `front_road_edge_overlay`: hiển thị hai biên đường.
3. `front_road_departure_advisory`: cảnh báo gần biên đường sau khi có policy và
   fixture acceptance riêng.
4. `front_visual_odometry`: xuất pose và road transform làm telemetry, đồng thời
   hỗ trợ phát hiện camera bị xê dịch.
5. `front_model_confidence`: công bố uncertainty, continuity và confidence thay
   vì chỉ trạng thái `READY`/`NOT_READY`.

## Các chức năng openpilot không cung cấp

Openpilot hiện không cung cấp detector độc lập cho:

- biển báo giao thông hoặc giới hạn tốc độ;
- đèn giao thông hoặc đèn đỏ;
- điểm mù và va chạm bên hông;
- phân loại cảnh báo riêng cho người đi bộ hoặc xe đạp;
- Automatic Emergency Braking (AEB) do openpilot tự điều khiển.

## Tham chiếu source

- Contract output của model: [openpilot `ModelDataV2`](../../.tmp/openpilot/openpilot/cereal/log.capnp).
- Parser output upstream: [openpilot `parse_model_outputs.py`](../../.tmp/openpilot/openpilot/selfdrive/modeld/parse_model_outputs.py).
- Adapter LS-Vision: [`openpilot_front_engine.py`](../../apps/src/adapters/models/openpilot_front_engine.py).
- Domain contract và alert policy: [`front_assistance.py`](../../apps/src/domain/front_assistance.py).
- Projection overlay: [`front_overlay.py`](../../apps/src/domain/front_overlay.py).
- Giới hạn chức năng upstream: [openpilot `LIMITATIONS.md`](../../.tmp/openpilot/docs/LIMITATIONS.md).

Thư mục `.tmp/openpilot` chỉ là source tham khảo cục bộ, không thuộc runtime
canonical hoặc release source của LS-Vision.
