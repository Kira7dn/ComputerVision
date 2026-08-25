# Camera workspace

Workspace có hai boundary độc lập:

- `apps/`: runtime source và web của ứng dụng multi-camera DeepStream.
- `training/`: chuẩn bị dataset, huấn luyện và kiểm tra model offline.
- `services/camera-server/`: dịch vụ Dahua ADAS/FTP/archive có package, dependency và test riêng.

## LS-Vision

```powershell
npm install --prefix apps/web
npm test
npm run check
npm run dev
npm run deploy
```

Runtime Python nằm trong package `apps/src`. Production vẫn đóng gói ứng dụng dưới `/opt/ls-vision/releases/<release>/app` để tương thích rollback với các release cũ.

Các layer runtime trực tiếp là `domain`, `application`, `adapters`, `interfaces` và `bootstrap`;
không có namespace bọc ngoài. `media_only` là feed playback, không tạo vision worker.

`pyproject.toml` cùng requirements của LS-Vision nằm tại root. Camera Server tiếp tục sở hữu
package và dependency độc lập. Xem [kiến trúc Platform](docs/architecture/Platform.md).

## Camera Server

```powershell
npm run test:camera-server
& .\.venv\Scripts\python.exe -m camera_server.main
```

Lệnh chạy trực tiếp cần working directory `services/camera-server` hoặc cài package từ chính thư mục này. Camera Server không thuộc startup path hay acceptance gate production của LS-Vision.
