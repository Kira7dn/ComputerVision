# Camera Deployment Kit

Runtime có một entrypoint, một file cấu hình và một file secret:

```text
deploy/run.ps1       # production, development, acceptance và build commands
deploy/config.yaml   # toàn bộ cấu hình runtime và Frigate
.env.local           # credential, được Docker Compose nạp trực tiếp
```

Các file Compose nội bộ nằm trong `deploy/reference/`; người vận hành
không cần sửa chúng.

## Yêu cầu

- Windows PowerShell 5.1 trở lên.
- Docker Desktop với Docker Compose và NVIDIA GPU support.
- Python 3 có package `PyYAML`.
- `ffmpeg` và `ffprobe` trong `PATH`.
- Image được khai báo tại `runtime.image` và model tại `runtime.model_path` đã tồn tại.
- Root `.env.local` tồn tại. File này chỉ chứa secret và đã được Git ignore.

Ngrok agent cũng do Compose quản lý. Khai báo `NGROK_AUTHTOKEN` và reserved
`NGROK_URL` trong `.env.local`; cổng Agent API 4040 chỉ nằm trong network Compose,
không được publish ra host. Tunnel đi vào cổng Frigate 8971 có authentication,
riêng media artifact dùng URL ký HMAC và chỉ cho phép GET.

## Sử dụng

Chạy từ `D:\BusinessAnalyze\Camera`:

```powershell
.\deploy\run.ps1 doctor
.\deploy\run.ps1 start
.\deploy\run.ps1 status
.\deploy\run.ps1 logs
.\deploy\run.ps1 stop
.\deploy\run.ps1 build
```

## Development không build image

Các lệnh development bind-mount trực tiếp package Python local vào container ở
`/opt/frigate/frigate` theo chế độ read-only. Sửa source không cần build lại
frontend hoặc Docker image; restart chỉ recreate service Frigate và giữ nguyên
model/media/cache hiện có.

```powershell
# Khởi động runtime development lần đầu
.\deploy\run.ps1 dev-start

# Sau mỗi lần sửa Python
.\deploy\run.ps1 dev-restart

# Theo dõi log Frigate; Ctrl+C chỉ dừng việc theo dõi log
.\deploy\run.ps1 dev-logs

# Dừng toàn bộ runtime development
.\deploy\run.ps1 dev-stop
```

Mặc định source là `frigate/frigate`. Có thể chỉ định checkout khác bằng
`-SourceDir`, nhưng thư mục đó phải là package Frigate có file `__init__.py`:

```powershell
.\deploy\run.ps1 dev-start -SourceDir D:\path\to\frigate\frigate
```

Dev mode không tự reload process Python. Sau khi sửa code phải chạy
`dev-restart`; lệnh này luôn dùng `--no-build`.

Không có tham số source, loop hoặc detector. Sửa các giá trị đó trong
`deploy/config.yaml`.

`build` đóng image tại `runtime.image` từ `runtime.build_base_image`, source Python,
frontend và Nginx hiện tại. Đây là build overlay có giới hạn cứng 5 phút:

- Base image bắt buộc đã tồn tại local; script không tự pull hoặc fallback sang full build.
- `Dockerfile.runtime` chỉ được có một `FROM` và các lệnh `COPY`; `RUN`/`ADD` bị từ chối.
- Quá 5 phút, script dừng toàn bộ cây tiến trình build và trả lỗi.
- CUDA, Intel Media Driver và các dependency hệ thống không bao giờ được biên dịch bởi bộ deploy này.

Full dependency image phải được đóng riêng trong pipeline release rồi chuyển tới máy deploy dưới
dạng image đã kiểm duyệt. Không dùng `deploy/run.ps1` để full-build Frigate.

## Nhiều camera

Mỗi camera phải có cùng một tên tại `go2rtc.streams` và `cameras`.

RTSP production:

```yaml
go2rtc:
  streams:
    gate_camera:
      - rtsp://user:password@camera/stream

cameras:
  gate_camera:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/gate_camera
          input_args: preset-rtsp-restream
          roles: [detect, record]
```

File replay được khai báo thêm trong `runtime.replay.sources`; đường dẫn tương đối được
tính từ root workspace:

```yaml
runtime:
  replay:
    loop: true
    sources:
      face_camera: videos/face.mp4
      gate_camera: videos/gate.mp4

go2rtc:
  streams:
    face_camera:
      - rtsp://mediamtx:18554/face_camera
    gate_camera:
      - rtsp://mediamtx:18554/gate_camera
```

Có thể trộn RTSP và replay trong cùng một runtime. Mỗi source phải là H.264 và giải mã
được ít nhất một frame; `doctor` kiểm tra tất cả source nhưng không start container.
Replay dùng stream-copy để không tốn CPU chuyển mã lúc chạy, vì vậy cần chuẩn hóa trước
độ phân giải và FPS của file nguồn theo profile camera mong muốn.

Profile hiện tại giữ `face_camera` ở 1280×720/5 FPS và chạy `car_camera` từ nguồn
1820×1024 với detect 1820×1024/5 FPS. Fixture passage khai báo kích thước riêng cho
từng camera; bbox/ROI phải dùng hệ tọa độ của đúng detect frame, không dùng chung tọa
độ 720p cho LPR.

## Kiểm tra face recognition

Thư viện khuôn mặt nằm dưới `runtime.media_dir/clips/faces/<identity>`. Khi runtime
khởi động, log báo số identity và số ảnh training thực sự được nạp. Xem log bằng:

```powershell
.\deploy\run.ps1 logs
```

Cứ 30 giây sẽ có một dòng `Face recognition pipeline` với các bộ đếm chính:

- `no_face`, `too_small`: chưa có crop mặt đủ điều kiện.
- `classifier_unavailable`: classifier chưa sẵn sàng hoặc thư viện rỗng.
- `unknown`, `matched`: kết quả phân loại dưới/trên ngưỡng.
- `vote_pending`, `snapshot_queued`: trạng thái voting và snapshot chờ commit.

Dòng `Face snapshot metrics` xác nhận bước ghi media/DB; `committed` phải tăng và
`failed` phải bằng `0` khi nhận diện thành công.

## Notification event

Thiết kế SOT, canonical media và lộ trình cải thiện được ghi tại
[`Platform.md`](../docs/architecture/Platform.md).

Pipeline notification và các channel WebPush, Telegram, Zalo vẫn được cấu hình, nhưng
listener được điều khiển riêng bằng từng phần tử trong `notifications.rules`.
`notifications.pipeline.shadow_mode` đang là `false`; không cần sửa giá trị này khi bật
lại một rule.

Trạng thái bàn giao hiện tại: tất cả listener bên dưới đều đang `enabled: false`:

- `car_alert`
- `car_license_plate`
- `car_semantic_trigger`
- `car_monitoring`
- `face_recognition`

Việc tắt rule chỉ ngừng tạo notification delivery. Camera, detector, LPR, face
recognition, Event SOT, evidence và canonical artifact vẫn tiếp tục hoạt động bình
thường.

Để bật notification cho một loại event, đổi đúng rule cần dùng về `enabled: true`, sau
đó áp dụng cấu hình:

```powershell
.\deploy\run.ps1 start
```

Ví dụ bật lại face recognition:

```yaml
- id: face_recognition
  enabled: true
  event: face_recognized
  filters:
    cameras: [face_camera]
    labels: [person]
    identities: ['*']
```

`identities: ['*']` chỉ cho phép identity đã nhận diện; kết quả `unknown` không được
gửi. Rule hiện có `cooldown: 0`, vì vậy mỗi event hoàn tất đủ điều kiện đều có thể tạo
một notification. Muốn tắt lại nhưng giữ cấu hình để dùng lần sau, chỉ đổi
`enabled: false`; không xóa rule và không cần tắt provider.

Notification sử dụng đúng canonical artifact đã ghim theo event revision. Caption mặc
định hiện tại:

```text
👤 <identity> · face_camera
Đã nhận diện khuôn mặt · Tin cậy <score>%

🚗 <license_plate> · car_camera
Xe đã kết thúc lượt qua · Tin cậy <score>%
```

Các rule cùng event được coalesce theo recipient/channel; Telegram và Zalo của cùng
revision sử dụng chung `media_artifact_id`, không render ảnh riêng theo provider.

## Đăng nhập Frigate

- URL: `https://localhost:8971/login`
- Username: `admin`
- Password: `123456`

Password này được lưu dạng plaintext theo yêu cầu vận hành. Sau khi đổi password
trên dashboard, cần cập nhật lại mục này nếu vẫn muốn README là tài liệu bàn giao.

## Secret

Telegram và Zalo là provider native của Frigate. Token chỉ được đọc từ root
`.env.local` qua các biến sau:

```dotenv
FRIGATE_TELEGRAM_BOT_TOKEN=
FRIGATE_ZALO_BOT_TOKEN=
NGROK_AUTHTOKEN=
NGROK_URL=https://example.ngrok.app
```

Không đặt secret vào `deploy/config.yaml`. Credential trong RTSP URL được che trong
status, log và exception.

## File nội bộ

`run.ps1` chỉ sinh state và Compose override cho số replay service động:

```text
.tmp/runtime/state.json
.tmp/runtime/compose.replay.yml
```

Không sinh bản sao cấu hình Frigate. `deploy/config.yaml` là SOT duy nhất và được
mount đọc/ghi trực tiếp thành `/config/config.yml` trong container. Save trên
dashboard ghi chính file này; restart/deploy không copy hoặc ghi đè từ nguồn khác.
Docker volume `/config` chỉ giữ database, model cache, JWT, outbox và các state khác.

## Cấu trúc

```text
deploy/
├── config.yaml
├── run.ps1
├── README.md
└── reference/
    ├── docker-compose.yml
    ├── mediamtx.replay.yml
```
