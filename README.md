# Camera workspace

Workspace có hai boundary độc lập:

- `app/`: LS-Vision multi-camera DeepStream runtime, dashboard và native Jetson deployment.
- `server/`: Dahua ADAS/FTP/archive service.

## LS-Vision

```powershell
npm install --prefix app/web
npm test
npm run check
npm run dev
npm run deploy
```

Runtime Python nằm trong package `app/src/ls_vision`. Production chạy từ source release versioned tại `/opt/ls-vision/releases`, với `current` là symlink atomic.

Xem [app/README.md](app/README.md) và [kiến trúc Platform](docs/architecture/Platform.md).

## Server boundary

`server/` giữ dependency, package và test riêng theo root `pyproject.toml`. Thay đổi LS-Vision không được kéo `server/` vào startup path hoặc acceptance gate.
