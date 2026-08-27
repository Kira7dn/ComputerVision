# Tính năng camera trước từ openpilot

## Phạm vi

Tài liệu này tổng hợp các tính năng liên quan tới camera nhìn đường phía trước của
openpilot và mức độ có thể sử dụng độc lập trong LS-Vision. Các output perception
dùng chung một phiên inference `driving_supercombo`; không tạo một model session
riêng cho từng tính năng.

Model openpilot đã đối chiếu có SHA-256
`659727c4d4839adc4992a254409a54259a8756a743f2d567bf5fdc6579f8009b`, trùng với
model đang được pin bởi bài kiểm tra front-camera offline của LS-Vision.

Với synchronized production mock, front engine reset toàn bộ recurrent image,
feature, brake và confidence state tại mỗi biên chu kỳ video 191,1 giây. RTCP
timestamp của publisher vẫn tăng liên tục nên biên này được xác định từ
`mock_sync_period_seconds`/`mock_sync_epoch_seconds`, không chờ timestamp gap.

## Tiến độ triển khai ngày 27/08/2026

- ✅ Section 1 camera-only đã hoàn tất implementation và được lưu tại commit
  `2687935` trên `origin/codex/dashboard-ui-compact`.
- ✅ Production release `release-20260827-133344` đang active trên Jetson; health
  nội bộ và `http://vision.local/health/ready` trả HTTP 200.
- ✅ Camera trước publish video có lane, road edge, path corridor và lead chevron.
  HUD đã được rút gọn: không còn các panel thông số hoặc nhãn horizon; khi có
  alert chỉ hiện đúng một banner tiếng Việt được bake trực tiếp vào video.
- ✅ Alert camera-only đã nối vào event/evidence pipeline với lifecycle
  `START/END`, snapshot và thumbnail; không nối notification hoặc vehicle
  actuation.
- ✅ Acceptance hiện tại đạt 224 bài pytest, Ruff, compileall, web build/lint và
  kiểm tra browser thật tại `http://vision.local`: 5/5 camera ready, video phát,
  trạng thái không alert không có chữ thừa và trạng thái alert chỉ có một banner.
- ⚠️ Acceptance này dùng synchronized production mock và synthetic fixtures;
  chưa phải vehicle-calibrated acceptance với CAN/sensor và xe thật.

## Bảng tính năng

Quy ước: `✅ Done` đã hoàn tất trong LS-Vision; `🚧 Doing` đã có một phần pipeline
hoặc contract; `⬜ Not yet` chưa triển khai trong LS-Vision.

### 1. Camera-only

Các tính năng trong nhóm này có thể chạy từ luồng camera, model và calibration
được provision trước, không bắt buộc kết nối CAN hoặc cảm biến xe ở runtime.

| Status | Tính năng | Mức độc lập | Trạng thái LS-Vision hiện tại |
| --- | --- | --- | --- |
| ✅ Done | Nhận diện 4 vạch làn và độ tin cậy | Cao, chỉ cần inference và calibration | Parse và render đủ 4 vạch; opacity bám theo probability của từng vạch |
| ✅ Done | Dự đoán quỹ đạo xe phía trước | Cao cho hiển thị và telemetry | Đã render hành lang và đường tâm dự đoán |
| ✅ Done | Cảnh báo lệch làn trái/phải (LDW) | Khá độc lập; bản openpilot đầy đủ còn dùng tốc độ xe, xi-nhan và trạng thái lateral control | Đã có policy camera-only `vision_ldw_left` và `vision_ldw_right` |
| ✅ Done | Cảnh báo va chạm phía trước từ model (FCW) | Khá độc lập, nhánh `hardBrakePredicted` không bắt buộc radar | Đã có policy camera-only `vision_fcw` |
| ✅ Done | Nhận biết tối đa 3 xe dẫn đầu | Cao cho perception và overlay; điều khiển bám xe không độc lập | Contract v2 và metadata giữ đủ 3 lead cùng std; video dùng chevron đỏ/glow vàng kiểu openpilot cho tối đa 2 lead có probability ≥ 0.5, không giả lập bbox; `vision_lead_ttc` là advisory camera-only |
| ✅ Done | Nhận biết 2 biên đường | Cao cho hiển thị và phân tích | Render đủ 2 biên với opacity theo uncertainty; `vision_road_edge_left/right` dùng clearance 5–30 m và hysteresis |
| ✅ Done | Visual odometry: chuyển động tịnh tiến và quay của camera | Có thể xuất telemetry độc lập | `pose` và std được công bố trong `FrontPerception` v2 và metadata API; không hiển thị panel thông số trên video |
| ✅ Done | Ước lượng camera mounting và road transform | Có thể dùng để chẩn đoán camera bị xê dịch; tự calibration đầy đủ còn cần vận tốc xe | Công bố Euler/road transform và std; `vision_geometry_drift` học baseline theo epoch và được gắn `experimental_advisory=true` |
| ✅ Done | Vị trí, vận tốc, gia tốc, hướng và tốc độ quay dự đoán trong 10 giây | Độc lập cho telemetry; không độc lập cho điều khiển xe | Contract v2 giữ đủ 15 thành phần plan, std và sáu horizon gần 0/2/4/6/8/10 giây; video chỉ giữ marker hình học, không ghi nhãn giây |
| ✅ Done | Confidence, disengagement và dự đoán hành vi ga/phanh | Có thể dùng cho diagnostics | Công bố toàn bộ 55 meta probabilities, desire state/prediction và confidence green/yellow/red trong metadata API; không bake panel chẩn đoán lên video |
| 🚧 Doing | Sức khỏe và đồng bộ đầu ra model | Hữu ích cho telemetry, không phải perception mới | Đã có source timestamp, inference time, provider và frame number; chưa công bố riêng `frameAge` và `frameDropPerc` như `modelV2` upstream |
| ✅ Done | Ghi hình, snapshot, thumbnail và livestream | Độc lập về media | Openpilot có sẵn, nhưng LS-Vision và MediaMTX đã sở hữu các chức năng tương đương; không thêm owner thứ hai |

Wide-road camera của openpilot là một **video stream thứ hai**, khác với
`wide_from_device_euler` (chỉ là output pose). LS-Vision hiện chỉ sở hữu
`camera_front` và không bật cơ chế chuyển narrow/wide hoặc thêm sensor input, nên
không xem wide-road camera switching là tính năng đã triển khai.

LDW camera-only của LS-Vision là advisory dựa trên model. So với openpilot đầy
đủ, policy này chưa dùng tốc độ xe, trạng thái xi-nhan hoặc trạng thái lateral
control. Tương tự, camera mounting và road transform có thể xuất trực tiếp từ
model, nhưng quy trình tự calibration đầy đủ cần thêm vận tốc xe.

Trong `ModelDataV2`, `timestampEof` và `modelExecutionTime` tương ứng với
timestamp/inference time hiện có của LS-Vision; `frameAge` và `frameDropPerc` là
hai chỉ số đồng bộ model chưa được đưa thành field riêng trong contract. Output
`action` cũng không phải raw perception độc lập: upstream suy ra nó từ plan,
độ trễ và tốc độ xe, vì vậy được phân loại ở nhóm Camera + CAN/Sensor.

Về hình ảnh, openpilot còn đổi màu path theo trạng thái throttle/experimental
và có thể rút ngắn path khi lead ở gần. LS-Vision hiện luôn vẽ corridor model
với màu cố định; đây là khác biệt trình bày có chủ đích, không phải thiếu output
perception hay cảnh báo an toàn.

Toàn bộ cảnh báo front-assistance chỉ là camera-only advisory: khi active, cảnh
báo có một banner tiếng Việt bake vào RTSP/HLS và tạo evidence `START/END` kèm
snapshot/thumbnail; chúng không đi vào notification pipeline và không có vehicle
actuation. Acceptance của Section 1 dùng mock video production
hiện tại cùng synthetic policy fixtures. Theo yêu cầu vận hành mock, cấu hình
`dev` và `production` dùng profile độ nhạy đã hiệu chỉnh từ mock replay để event
tự nhiên xuất hiện nhưng không lặp quá dày: START xác nhận một frame, END cần 20
frame âm tính để gộp dao động ngắn thành một episode, và chấp nhận khoảng gián
đoạn timestamp tối đa 500 ms để bao phủ nguồn có độ trễ tới 400 ms. Policy không
dùng cooldown cố định; mỗi loại rearm theo chính tín hiệu trigger/clear. Riêng geometry drift giữ
ngưỡng chuẩn: baseline 200 frame, translation 0,25 m, roll/pitch 2°, yaw 3°,
xác nhận 40/50 frame và clear sau 100 frame. Cấu hình này loại dao động nhỏ của
mock/calibration khỏi danh sách event. Profile camera-only vẫn không phải
vehicle-calibrated production acceptance.

Dashboard phân loại `vision_fcw` là `Nguy hiểm`; lead TTC, LDW và road-edge là
`Cảnh báo`; geometry drift là `Sự kiện` chẩn đoán. Phân loại hiển thị này không
thay đổi classification kỹ thuật hoặc lifecycle evidence `START/END`.

### 2. Camera + CAN/Sensor

Các tính năng trong nhóm này dùng output camera nhưng không thể vận hành an toàn
chỉ từ video. Chúng cần trạng thái xe, CAN, cảm biến hoặc vehicle integration.

| Status | Tính năng | Dependency bổ sung | Trạng thái LS-Vision hiện tại |
| --- | --- | --- | --- |
| ⬜ Not yet | Automated Lane Centering (ALC) | Tốc độ, trạng thái lái, vehicle model, CAN controller và safety layer | Chưa thuộc camera-only runtime |
| ⬜ Not yet | Lane change assist | Xi-nhan, tương tác người lái, lateral control và vehicle integration | `desireState` camera-only đã có trong metadata; `laneChangeState`/`laneChangeDirection` của openpilot vẫn cần car state và lateral control |
| ⬜ Not yet | Adaptive Cruise Control (ACC) | `carState`, cruise state, radar/vision fusion, longitudinal planner và CAN safety | Chưa triển khai |
| ⬜ Not yet | Model action output (curvature/acceleration/stop) | `vEgo`, action delay, longitudinal/lateral controller và CAN safety | openpilot có `ModelDataV2.action` (`desiredCurvature`, `desiredAcceleration`, `shouldStop`); LS-Vision chỉ giữ plan và không phát lệnh điều khiển |

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

Các consumer độc lập đã được triển khai trên cùng `FrontPerception` v2 gồm lead
overlay/TTC, road-edge overlay/departure advisory, visual odometry/geometry drift
và model confidence. Chúng không mở thêm model session.

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
