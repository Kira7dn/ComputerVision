# PRD LS-Vision Camera AI

## Mục tiêu

LS-Vision cung cấp phân tích camera AI on-premise cho nhà máy, kho vận và phương tiện, chạy native trên Jetson, không phụ thuộc cloud cho inference hoặc evidence.

## Người dùng

- Operator theo dõi live feeds và cảnh báo.
- EHS/security xem event và evidence.
- Kỹ sư vận hành cấu hình camera/model và quản lý release.

## Functional requirements

- Nhận RTSP camera và synchronized mock fixtures theo profile.
- Chạy function theo từng camera: DMS, face recognition, smoking, fire/smoke và front assistance.
- Giữ frame lineage, bbox, score, temporal confirmation và lifecycle của event.
- Persist evidence, event journal, idempotency state và durable notification outbox.
- Cung cấp dashboard live, health, metrics, event list/detail và evidence endpoints.
- Xuất RTSP/HLS/WebRTC qua MediaMTX.
- Hỗ trợ native source deploy, status và rollback không làm mất runtime data.

## Non-functional requirements

- Một vision process chỉ sở hữu một camera.
- Input/output và analysis queues phải bounded; live input ưu tiên freshness.
- Model/provider/path/config phải validate fail-closed trước startup.
- Secret không được commit, log hoặc ghi vào event/evidence.
- Child process failure phải làm service owner thoát để systemd recovery.
- Production artifact phải truy được source commit, config hash và model checksum.

## Acceptance

- Hai config profile standalone và cùng camera topology.
- Unit/static/package/frontend gates pass.
- Native Jetson services active và release manifest hợp lệ.
- Health/API/topology/media contracts pass trên target.
- Event/evidence còn hoạt động sau restart.
- Browser thật tại `vision.local` phát được các feed theo contract hiện hữu.
- Endpoint model/GPU thật được đánh giá riêng; mock acceptance không thay thế production model acceptance.

## Ngoài phạm vi architecture cleanup

- Thay camera topology hoặc media-only behavior.
- Thay model, threshold, confirmation policy hoặc event schema.
- Thay đổi boundary `services/camera-server/`.
