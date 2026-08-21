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

## DeepStream Safety runtime

Safety live now runs as a standalone DeepStream pipeline. It does not import Frigate,
read Frigate configuration, or require Docker.

Source code:

```text
app/src/application/camera_worker.py
app/config/dev.yaml
app/deploy/powershell/start.ps1
app/web/dashboard.html
app/deploy/docker/mediamtx.yml
```

The source stays in this workspace. WSL Ubuntu-22.04 provides the DeepStream runtime;
MediaMTX provides RTSP and WebRTC/HLS transport.

```powershell
# Start Vite HMR, backend hot reload, mock RTSP input, DeepStream inference, MediaMTX and the API
npm run wsl:start

# Reattach logs, check status, pause the runtime, or shut down all WSL distros
npm run wsl:logs
npm run wsl:status
npm run wsl:pause
npm run wsl:stop
```

`wsl:start` remains attached and streams backend, MediaMTX, hot-reload, and Vite logs. Pressing
`Ctrl+C` detaches the log stream without stopping the runtime; use `wsl:logs` to reattach.
`wsl:pause` gracefully stops LS-Vision, MediaMTX, mock publishers, API, and Vite while keeping
Ubuntu running; use `wsl:start` to resume. `wsl:stop` additionally calls `wsl --shutdown`, which
stops every WSL distro including the Docker Desktop WSL backend.

Open the dashboard at:

```text
http://127.0.0.1:5173/dashboard.html
```

The WSL dashboard API and health endpoints remain available at
`http://127.0.0.1:18080`; production serves the built dashboard bundle there.

During native WSL development, Vite hot reloads `app/web`. The backend supervisor watches
`app/src`, `app/config`, `.env.local` and the MediaMTX development config, restarting only the
affected runtime processes when those files change.

The annotated stream is published as `rtsp://127.0.0.1:8554/safety_bbox` and is
played in the browser through MediaMTX HLS. Bbox/label rendering is owned by
DeepStream `nvdsosd` on the encoded frame; the dashboard does not draw a second
Canvas overlay. Detection metadata is also published on ZeroMQ
`tcp://127.0.0.1:5555` and exposed for monitoring consumers.

Snapshots are enabled in `app/config/dev.yaml` and are written only when
a detection is present, at most once per second:

```text
.tmp/deepstream-safety/snapshots
```

Detailed setup and troubleshooting is in `docs/DeepStream.md`.
