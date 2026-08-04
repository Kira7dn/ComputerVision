# Dahua local proof-of-concept

Hai boundary độc lập:

```text
control/server.py (:8081) → dashboard/backup control; không nhận media
Dahua NetSDK TS/FTP → media/server.py (:8080) + media/ftp_receiver.py (:2121)
```

Khởi động từ thư mục `server`:

```powershell
python control/server.py --host 0.0.0.0 --port 8081
python media/server.py --public-host 192.168.100.108
python media/ftp_receiver.py --public-host 192.168.100.108
```

Live dùng API chính thức `RealPlayByDataType(TS)` của Dahua NetSDK. MediaServer giữ
login/play handle, nhận TS qua callback có queue giới hạn và đẩy vào FFmpeg để tạo HLS;
không tự dựng RTSP, SDP hoặc multicast port. Có thể đặt mật khẩu bằng
`$env:DAHUA_PASSWORD`; mặc định môi trường test hiện tại là `letron123`.

Runtime media luôn nằm trong `uploads`. Video backup thật nằm tại `uploads/videos`; HLS trong
`uploads/live` là dữ liệu tạm và được tạo lại theo từng live session.

## Edge spool lên cloud

FTP receiver enqueue file ngay sau khi Dahua hoàn tất `STOR`. Uploader chạy độc lập, giữ queue
trong SQLite và không xóa source khi upload lỗi:

```powershell
# Local integration backend
python media/cloud_uploader.py --backend local-test --once

# AWS S3
python media/cloud_uploader.py --backend s3 `
  --bucket <bucket> `
  --prefix dahua-history/<vehicle-id>

# Xem durable queue
python media/cloud_uploader.py --backend local-test --status
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
./media/run_netsdk_backup.ps1 -Mode latest

# Worker định kỳ mỗi 5 phút, tải tất cả file chưa tồn tại trong 24 giờ gần nhất
./media/run_netsdk_backup.ps1 -Mode all -IntervalSeconds 300
```

Video được ghi trực tiếp vào `uploads/videos`. Có thể chạy `cloud_uploader.py`
song song; queue sẽ reconcile file mới và upload lên object storage mà không cần
ESP32 giao tiếp với MediaServer.

## ADAS detection trên RTX 3050

ADAS là feature flag và không ảnh hưởng HLS khi chưa bật. Bootstrap Python 3.12/TensorRT,
build engine rồi chạy MediaServer cùng MediaMTX:

```powershell
./media/setup_adas.ps1
./.venv-adas/Scripts/python.exe media/build_adas_engine.py
$env:DAHUA_PASSWORD='<xvr-password>'
./media/run_adas.ps1
```

Mặc định ADAS dùng channel 2, main stream (subtype 0, có OSD Channel/Time), latest-frame capacity 1 và publish annotated
H.264 tới `rtsp://127.0.0.1:8554/adas-ch2`. Browser mở
`http://192.168.100.108:8080/?channel=2`; Media UI ưu tiên WebRTC
`http://192.168.100.108:8889/adas-ch2` và chỉ dùng HLS làm fallback.

Các biến cấu hình:

```text
ADAS_ENABLED=true
ADAS_CHANNELS=2
ADAS_MODEL_PATH=<server>/models/yolov8n.engine
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
