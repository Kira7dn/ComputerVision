# THƯ BÀN GIAO VISION STREAM ENDPOINT
**Kính gửi:** Team TBox  
LS-Vision bàn giao 6/6 stream READY trên production `release-20260826-182649`; native E2E `accepted=true`.
**Discovery trên Jetson:** `GET http://127.0.0.1:18080/api/v1/streams`
**Discovery trong LAN:** `GET http://vision.local/api/v1/streams`
| Camera | RTSP trên Jetson | WHEP trên LAN | Video |
|---|---|---|---|
| `DMS` | `rtsp://127.0.0.1:8554/dahua_bbox` | `http://vision.local:8889/dahua_bbox/whep` | H.264 960x540 10 FPS |
| `camera_front` | `rtsp://127.0.0.1:8554/camera_front` | `http://vision.local:8889/camera_front/whep` | H.264 960x540 20 FPS |
| `camera_back` | `rtsp://127.0.0.1:8554/camera_back` | `http://vision.local:8889/camera_back/whep` | H.264 960x540 10 FPS |
| `camera_left` | `rtsp://127.0.0.1:8554/camera_left` | `http://vision.local:8889/camera_left/whep` | H.264 960x540 10 FPS |
| `camera_right` | `rtsp://127.0.0.1:8554/camera_right` | `http://vision.local:8889/camera_right/whep` | H.264 960x540 10 FPS |
| `camera_cargo` | `rtsp://127.0.0.1:8554/camera_back` | `http://vision.local:8889/camera_back/whep` | Alias `camera_back` |
TBox lấy `rtsp_path` từ manifest và chỉ dùng stream có `state=READY`, `published=true`, `codec=h264`.
RTSP là đầu vào service; WHEP chỉ dùng xem trên browser.
Vision chịu trách nhiệm discovery, publisher và media readiness; TBox chịu trách nhiệm MQTT và AWS Kinesis.
