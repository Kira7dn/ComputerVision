# Camera workspace runbook

## Quy tắc bắt buộc

- Khi đọc file có tiếng Việt, luôn dùng UTF-8.
- Làm việc từ PowerShell tại `D:\BusinessAnalyze\Camera`.
- Giữ nguyên thay đổi ngoài task; không dùng reset/checkout phá hủy hoặc xóa đệ quy ngoài thư mục tạm đã xác định.
- `services/camera-server/` là boundary ADAS/FTP/archive độc lập, không thuộc runtime LS-Vision.
- `apps/ls-vision/` là source, test, config và deployment owner duy nhất của LS-Vision.

## Runtime canonical

```text
apps/ls-vision/config/{dev,production}.yaml
  -> python -m ls_vision.service
  -> dashboard API + ls_vision.runner + optional mock media server
  -> one DeepStream process for each non-media-only camera
  -> native MediaMTX
```

`media_only` feeds không tạo vision worker. Không thay đổi topology, source, model hoặc function ownership nếu task không yêu cầu.

Production chạy source release native trên Jetson:

```powershell
npm run deploy
npm run deploy -- -Action status
npm run deploy -- -Action rollback
```

Development dùng isolated Jetson service và Vite HMR:

```powershell
npm install --prefix apps/ls-vision/web
npm run dev
```

Không dùng workspace khác để điều khiển `ls-vision*` hoặc `mediamtx.service`.

## Config và runtime data

- Chỉ `apps/ls-vision/config/dev.yaml` và `apps/ls-vision/config/production.yaml` là config source of truth.
- Hai profile phải standalone và giữ cùng camera order: `DMS`, `camera_front`, `camera_back`, `camera_left`, `camera_right`.
- Production root là `/opt/ls-vision`; development root là `/opt/ls-vision-dev`.
- Model và face library read-only; evidence/state/queue/log/status nằm ngoài source release.
- Secret chỉ lấy từ environment/secret file, không commit hoặc ghi vào evidence/status/log.

## Kiểm tra

```powershell
$python = 'D:\BusinessAnalyze\Camera\.venv\Scripts\python.exe'
& $python -m pytest -c apps/ls-vision/pyproject.toml apps/ls-vision/tests -q
& $python -m ruff check apps/ls-vision/src apps/ls-vision/tests
& $python -m compileall -q apps/ls-vision/src apps/ls-vision/tests
npm run check
git diff --check
```

Native production acceptance:

```powershell
& $python apps/ls-vision/tests/e2e/run_jetson_production_e2e.py `
  --jetson-alias jetson-nano `
  --report .tmp/ls-vision-native-e2e/summary.json
```

Chỉ gọi acceptance khi report có `accepted=true` và browser `http://vision.local` đã được kiểm tra.

## Git và bàn giao

- Stage bằng path đã xác nhận; không dùng `git add .`, `git add -A` hoặc `git add --all`.
- Phân biệt rõ test local, source release upload, service restart và verified production endpoint.
- Không xóa evidence/state hoặc release đang active.

## Tài liệu

- [Platform architecture](docs/architecture/Platform.md)
- [DeepStream runtime](docs/architecture/DeepStream.md)
- [Product requirements](docs/PRD.md)
- [Jetson deployment](apps/ls-vision/deploy/README-jetson.md)
- [Model inventory](docs/ModelInventoryAndFinetunePlan.md)
