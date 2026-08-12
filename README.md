# Dahua local proof-of-concept

Hai boundary độc lập:

```text
camera_server.main → control/media/ingest/archive runtime
```

Khởi động từ thư mục `server`:

```powershell
./start-all.ps1
```

Live dùng API chính thức `RealPlayByDataType(TS)` của Dahua NetSDK. MediaServer giữ
login/play handle, nhận TS qua callback có queue giới hạn và đưa trực tiếp vào pipeline ADAS;
không tự dựng RTSP, SDP hoặc multicast port. Có thể đặt mật khẩu bằng
`$env:DAHUA_PASSWORD`; mặc định môi trường test hiện tại là `letron123`.

Runtime media luôn nằm trong `uploads`. Video backup thật nằm tại `uploads/videos`; dữ liệu live
chỉ phục vụ pipeline ADAS và WebRTC.

## Edge spool và local archive

FTP receiver enqueue file ngay sau khi Dahua hoàn tất `STOR`. Uploader chạy độc lập, giữ queue
trong SQLite và không xóa source khi upload lỗi:

```powershell
# Durable worker chạy foreground, dùng cùng runtime/queue với FTP receiver
python -m camera_server.archive.service `
  --spool "$env:CAMERA_RUNTIME_DIR/uploads/videos" `
  --queue-db "$env:CAMERA_RUNTIME_DIR/queue/cloud_queue.sqlite3" `
  --target "$env:CAMERA_RUNTIME_DIR/uploads/cloud-test"
```

Test failure/retry/restart/idempotency:

```powershell
python -m unittest discover -s tests -v
```

## Kéo recording HDD bằng Dahua NetSDK

Đây là flow tương đương Hik.Web: MediaServer đăng nhập cổng NetSDK `37777`, gọi
`CLIENT_QueryRecordFile`, bỏ file cuối đang có thể còn được ghi, rồi gọi
`CLIENT_DownloadByRecordFile`. XVR không tạo recording mới và không cần FTP push.

```powershell
$env:DAHUA_PASSWORD='<xvr-password>'

# Tải recording hoàn chỉnh mới nhất của 8 channel rồi thoát
./deploy/run-backup.ps1 -Mode latest

# Worker định kỳ mỗi 5 phút, tải tất cả file chưa tồn tại trong 24 giờ gần nhất
./deploy/run-backup.ps1 -Mode all -IntervalSeconds 300
```

Video được ghi trực tiếp vào runtime spool. `worker.py` chạy song song; queue sẽ
reconcile file mới và sao lưu vào local archive mà không cần ESP32 giao tiếp với
MediaServer.

## ADAS detection trên RTX 3050

ADAS là đường hiển thị duy nhất của live UI. Bootstrap Python 3.12/TensorRT,
build engine rồi chạy MediaServer cùng MediaMTX:

```powershell
./.venv/Scripts/python.exe server/camera_server/tools/build_adas_engine.py
$env:DAHUA_PASSWORD='<xvr-password>'
./deploy/run-adas.ps1
```

Mặc định ADAS dùng channel 2, main stream (subtype 0, có OSD Channel/Time), latest-frame capacity 1 và publish annotated
H.264 tới MediaMTX và WebRTC. Browser mở duy nhất
`http://192.168.100.108:8080/?channel=2`; UI chỉ gắn WebRTC khi ADAS đã `healthy`.

Các biến cấu hình:

```text
ADAS_ENABLED=true
ADAS_CHANNELS=2
ADAS_MODEL_PATH=<server>/assets/models/yolov8n.engine
MEDIAMTX_PUBLISH_URL=rtsp://127.0.0.1:8554/adas-ch2
MEDIAMTX_WEBRTC_BASE=http://192.168.100.108:8889
```

`/health` và `/api/state` trả counters, packet age, dropped stale frames và latency
decode-to-detection P50/P95/P99. SDK-to-detection vẫn được đánh dấu `verified=false` cho tới
khi encoded callback được correlate đúng với decoded-frame PTS. Trạng thái chỉ là `healthy`
sau TensorRT warm-up và khi packet mới không quá 150 ms.

Lưu ý kiểm định: `healthy` chỉ nghĩa là XVR đang trả stream NetSDK hợp lệ và pipeline xử lý
được; không chứng minh channel đang nối camera vật lý hoặc đang phát live thay vì black/test/
playback. SDK Python trong bộ này không có API seed video vào channel XVR; video giả phải đưa
vào MediaMTX/pipeline. `capture_to_browser_ms` chưa được đo vì TS callback không cung cấp PTS
camera usable.

## Frigate Camera runtime v2

Entrypoint duy nhất nằm tại `deploy/run.ps1`. Hướng dẫn đầy đủ nằm trong
`deploy/README.md`; toàn bộ cấu hình runtime và Frigate nằm trong `deploy/config.yaml`.
Production nhận RTSP H.264 trực tiếp qua go2rtc. Replay file bật MediaMTX cùng FFmpeg
trong Docker. Root `.env.local` chỉ chứa secret và được Docker Compose nạp trực tiếp.

```powershell
# Xem toàn bộ chức năng, không cần nhớ tên nhiều script
./deploy/run.ps1 help

./deploy/run.ps1 doctor
./deploy/run.ps1 start
./deploy/run.ps1 dev-start
./deploy/run.ps1 dev-restart
./deploy/run.ps1 status
./deploy/run.ps1 logs
./deploy/run.ps1 stop
```

Khai báo nhiều source trực tiếp trong `go2rtc.streams`, `cameras` và
`runtime.replay.sources`. Mỗi source phải là H.264 và giải mã được ít nhất một frame.
Runtime mount trực tiếp `deploy/config.yaml` thành `/config/config.yml`; không sinh thêm
một bản Frigate config hoặc runtime env trung gian.
