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

YAML function/model/policy changes được runner validate rồi áp dụng theo camera; xem `data/status/runner.json` để kiểm tra `config_generation`, `plan_hash`, `active_cameras`, model checksum, estimated inference rate, `last_restarted_cameras` và `reload_error`. Candidate lỗi không thay topology mà dashboard đang công bố. Thay đổi synchronized timeline bị từ chối khi hot reload và chỉ có hiệu lực sau restart service, tránh làm lệch clock giữa bốn mock camera.

Selective reload acceptance khi development service đang chạy:

```powershell
.\.venv\Scripts\python.exe tests\e2e\run_jetson_dynamic_pipeline_e2e.py `
  --jetson-alias jetson-nano `
  --report .tmp\ls-vision-dynamic-e2e\summary.json
```

Script thay đổi tạm DMS interval, xác nhận chỉ DMS đổi PID, front/timeline publisher giữ nguyên; sau đó inject YAML sai cú pháp để xác nhận generation/PID/dashboard active projection không đổi. Config được restore chính xác trước khi kết thúc.

## Acceptance

```powershell
.\.venv\Scripts\python.exe tests\e2e\run_jetson_production_e2e.py `
  --jetson-alias jetson-nano `
  --report .tmp\ls-vision-native-e2e\summary.json
```

Sau đó kiểm tra browser thật tại `http://vision.local/dashboard.html`.

Synchronized mock acceptance chỉ đạt khi `pipeline.mock_timeline.ready=true`, browser báo đủ bốn member locked liên tục 30 giây, p95 drift không quá 100 ms, max drift không quá 250 ms và re-lock trong 5 giây sau khi timeline process được restart riêng.
