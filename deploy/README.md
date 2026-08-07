# Camera Deployment Kit

Runtime có một entrypoint, một file cấu hình và một file secret:

```text
deploy/run.ps1       # start/status/logs/doctor/stop
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

## Secret

Telegram và Zalo là provider native của Frigate. Token chỉ được đọc từ root
`.env.local` qua các biến sau:

```dotenv
FRIGATE_TELEGRAM_BOT_TOKEN=
FRIGATE_ZALO_BOT_TOKEN=
```

Không đặt secret vào `deploy/config.yaml`. Credential trong RTSP URL được che trong
status, log và exception.

## File nội bộ

`run.ps1` chỉ sinh state và Compose override cho số replay service động:

```text
.tmp/runtime/state.json
.tmp/runtime/compose.replay.yml
```

Không sinh bản sao cấu hình Frigate. `deploy/config.yaml` được mount trực tiếp thành
`/config/config.yml` trong container.

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
