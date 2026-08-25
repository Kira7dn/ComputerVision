# Camera workspace

Workspace có hai boundary độc lập:

- `apps/ls-vision/`: ứng dụng multi-camera DeepStream, dashboard và native Jetson deployment.
- `services/camera-server/`: dịch vụ Dahua ADAS/FTP/archive có package, dependency và test riêng.

## LS-Vision

```powershell
npm install --prefix apps/ls-vision/web
npm test
npm run check
npm run dev
npm run deploy
```

Runtime Python nằm trong package `apps/ls-vision/src/ls_vision`. Production vẫn đóng gói ứng dụng dưới `/opt/ls-vision/releases/<release>/app` để tương thích rollback với các release cũ.

Xem [apps/ls-vision/README.md](apps/ls-vision/README.md) và [kiến trúc Platform](docs/architecture/Platform.md).

## Camera Server

```powershell
npm run test:camera-server
& .\.venv\Scripts\python.exe -m camera_server.main
```

Lệnh chạy trực tiếp cần working directory `services/camera-server` hoặc cài package từ chính thư mục này. Camera Server không thuộc startup path hay acceptance gate production của LS-Vision.
