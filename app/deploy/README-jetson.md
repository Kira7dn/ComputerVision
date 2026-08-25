# Native Jetson deployment

Camera workspace là owner duy nhất của `ls-vision.service`, `ls-vision-dev.service`, `ls-vision-ingress.service`, `ls-vision-mdns.service` và `mediamtx.service`.

## Production

```powershell
$env:LS_VISION_SUDO_PASSWORD = '<operator-secret>'
npm run deploy -- -JetsonAlias jetson-nano
npm run deploy -- -Action status -JetsonAlias jetson-nano
```

Deploy tạo source release versioned dưới `/opt/ls-vision/releases`, ghi `release-manifest.json`, đổi symlink `current` atomically và giữ hai release trước.

Rollback:

```powershell
npm run deploy -- -Action rollback -JetsonAlias jetson-nano
```

Rollback không xóa model, evidence, state hoặc log.

## Development

```powershell
npm run deploy -- -Development -JetsonAlias jetson-nano
npm run dev -- -JetsonAlias jetson-nano
```

Development dùng `/opt/ls-vision-dev`, port riêng và service bị disable khi kết thúc session. Production tiếp tục sở hữu `vision.local`.

## Acceptance

```powershell
.\.venv\Scripts\python.exe app\tests\e2e\run_jetson_production_e2e.py `
  --jetson-alias jetson-nano `
  --report .tmp\ls-vision-native-e2e\summary.json
```

Sau đó kiểm tra browser thật tại `http://vision.local/dashboard.html`.
