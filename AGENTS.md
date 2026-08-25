# Camera workspace runbook

## Quy tắc bắt buộc

- Khi đọc file có tiếng Việt, luôn đọc bằng UTF-8.
- Làm việc từ PowerShell tại D:\BusinessAnalyze\Camera.
- Giữ nguyên thay đổi không thuộc task; không dùng git reset --hard, git checkout --, hoặc xóa đệ quy ngoài thư mục tạm đã xác định.
- server/ là boundary ADAS/FTP/archive độc lập, không đưa vào thay đổi LS-Vision.
- frigate/ và kiến trúc Frigate cũ không phải startup path, media owner, event store, test gate hoặc source of truth của Camera runtime.

## Tài liệu dự án

- Kiến trúc tổng thể, ownership và trạng thái từng phase: [docs/architecture/Platform.md](docs/architecture/Platform.md)
- Kiến trúc runtime DeepStream hiện hành: [docs/architecture/DeepStream.md](docs/architecture/DeepStream.md)
- Yêu cầu sản phẩm tổng thể: [docs/PRD.md](docs/PRD.md)
- Use case nghiệp vụ Camera: [docs/CameraUseCase.md](docs/CameraUseCase.md)
- Cấu trúc và entrypoint của ứng dụng LS-Vision: [app/README.md](app/README.md)
- Deployment native và development trên Jetson: [app/deploy/README-jetson.md](app/deploy/README-jetson.md)
- Model inventory và kế hoạch fine-tune: [docs/ModelInventoryAndFinetunePlan.md](docs/ModelInventoryAndFinetunePlan.md)

Không đặt lại nội dung kiến trúc hoặc phase plan trong `AGENTS.md`; cập nhật nội dung đó tại
`docs/architecture/Platform.md` và chỉ giữ đường dẫn vận hành tại đây.

## Kiến trúc hiện tại

LS-Vision là runtime DeepStream canonical dưới app/:

```text
app/config/*.yaml -> app/src/runner.py -> một application.camera_worker cho mỗi camera
                   -> MediaMTX RTSP/WebRTC/HLS
app/src/interfaces/dashboard_api.py -> http://127.0.0.1:18080
```

Source Python nằm trực tiếp dưới app/src; không còn Python namespace cấp camera_safety:

```text
app/src/
├─ adapters/
├─ application/
├─ bootstrap/
├─ domain/
├─ interfaces/
├─ container.py
└─ runner.py
```

Import/entrypoint chuẩn là runner, container, application.camera_worker, interfaces.dashboard_api,
domain.*, adapters.*, và bootstrap.*. Không tạo lại import camera_safety.*. Chuỗi camera_safety
vẫn hợp lệ khi là camera ID trong config, metrics hoặc evidence path.

app/config/dev.yaml, production.yaml, e2e.yaml và config/cameras/ là source of truth cho camera ID,
input/output URL và function. runner.py tạo một run ID và một worker cho mỗi camera.
application.camera_worker sở hữu pipeline DeepStream, tracking/annotation, function dispatch và
RTSP output của một camera.

Production gồm hai Compose service:

```text
ls-vision  = dashboard/API + supervisor + một worker/camera
mediamtx   = RTSP/WebRTC/HLS
```

Models và face library là named volumes read-only. Evidence, SQLite state, queue và logs là named
volumes read-write. Runtime data không nằm trong source checkout.

## Shell, Python và ổ đĩa

```powershell
$cameraRoot = 'D:\BusinessAnalyze\Camera'
$python = Join-Path $cameraRoot '.venv\Scripts\python.exe'
Set-Location $cameraRoot
Get-Content -LiteralPath <path> -Encoding utf8
```

Dùng shared root interpreter .venv; không tạo virtual environment lồng trong app/.

Source code có thể nằm trên ổ D. Docker Desktop phải lưu disk image, build cache, image layers và
named volumes trên ổ E. Trước khi build/start production, kiểm tra Docker Desktop Settings ->
Resources -> Advanced -> Disk image location là ổ E. Không đặt Docker data, SQLite, evidence hoặc
model runtime bằng bind mount vào C:, D: hay /mnt/d; D: chỉ là build context/source checkout.

Production yêu cầu Docker Desktop, WSL2 integration và NVIDIA Container Toolkit/GPU support.
Không cài dependency runtime thủ công sau khi container đã chạy.

Jetson native production is a separate deployment target. The canonical source
is still `app/`, but the deployment contract is LeOS `tbox_lab`:

```powershell
# From Camera; wrapper around the LeOS deploy contract
npm run deploy

# Equivalent command from the LeOS repository
.\services\tbox\factory\tbox_lab.ps1 deploy-app `
  -JetsonAlias jetson-nano `
  -CameraRoot D:\BusinessAnalyze\Camera
```

Both commands deploy `app/` to the native Jetson `ls-vision.service` and do
not start Docker. For Nano or development deployment, pass arguments through
the single deploy script:

```powershell
npm run deploy -- -JetsonAlias jetson-nano
npm run deploy -- -Development -JetsonAlias jetson-nano
```

## Preflight

```powershell
git status --short
git branch --show-current
docker context show
docker info --format '{{.OSType}} {{.Architecture}}'
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -in @('python.exe', 'pytest.exe') -and $_.CommandLine -like '*BusinessAnalyze\Camera*' } |
  Select-Object ProcessId, Name, CommandLine
docker compose -f app\deploy\docker\compose.yaml ps
```

Sau run bị ngắt, chỉ dừng process Camera đã xác nhận là stale. Giữ failed evidence/log để chẩn đoán.
Không xóa .tmp khi runtime còn ghi; khi dọn chỉ nhắm đúng D:\BusinessAnalyze\Camera\.tmp sau khi đã
stop runtime.

## Configuration và secrets

- Development dùng app/config/dev.yaml; production dùng app/config/production.yaml; E2E dùng
  app/config/e2e.yaml qua compose.e2e.yaml.
- Production không bật mock input. Mock video chỉ dùng dev/E2E.
- Validate camera ID không trùng, URL hợp lệ, model/provider phù hợp và path runtime writable trước startup.
- Notification secrets chỉ lấy từ environment/secret file; không commit, log hoặc ghi vào event,
  manifest, status và evidence.
- Không đổi camera topology, model, confirmation gate, event semantics hoặc notification contract
  trong task restructure nếu chưa có yêu cầu riêng.

## Development on Jetson

Development runs on the isolated native Jetson service with local source sync
and Vite HMR:

```powershell
npm install --prefix app/web
npm run dev
```

For status, call the implementation directly:

```powershell
.\app\deploy\powershell\start-jetson-dev.ps1 -Action status -JetsonAlias jetson-nano
```

The `dev` launcher owns `jetson_sync.py`, the SSH API/MediaMTX tunnel and Vite.
Backend/config changes sync to Jetson and restart the isolated service; frontend
changes use Vite HMR. Stop the session with `Ctrl+C`.

Kiểm tra dev:

```powershell
(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5173/dashboard.html').StatusCode
(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/health/live').StatusCode
ssh jetson-nano "systemctl is-active ls-vision-dev.service"
```

## Production Docker Desktop

Compose production có GPU reservation, healthcheck, restart policy, network nội bộ, named volumes
và port operator bind localhost.

```powershell
docker compose -f app\deploy\docker\compose.yaml config --quiet
docker compose -f app\deploy\docker\compose.yaml build ls-vision
docker image inspect ls-vision:deepstream-7.1-gc-triton --format '{{json .Config.Entrypoint}}'
```

Entrypoint đúng phải là ["python3","-m","container"]. Nếu Docker Desktop chưa đặt storage trên ổ E,
dừng trước khi build.

```powershell
docker compose -f app\deploy\docker\compose.yaml up -d
docker compose -f app\deploy\docker\compose.yaml ps
docker compose -f app\deploy\docker\compose.yaml stop
```

Hoặc:

```powershell
docker compose -f app\deploy\docker\compose.yaml up -d
docker compose -f app\deploy\docker\compose.yaml ps
docker compose -f app\deploy\docker\compose.yaml stop
```

Production Docker is operated directly through Compose and is independent of the
three Jetson package scripts.
Không dùng deploy/run.ps1, Docker/Frigate cũ hoặc startup path ngoài Compose này.

## Test và static checks

```powershell
& $python -m pytest app/tests -q
& $python -m ruff check app/src app/tests
& $python -m compileall -q app/src app/tests

$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'app\deploy\powershell\start-jetson-dev.ps1'),
  [ref]$null,
  [ref]$parseErrors
) > $null
$parseErrors

Get-Content -LiteralPath 'package.json' -Raw -Encoding utf8 | ConvertFrom-Json > $null
git diff --check
```

Targeted test:

```powershell
& $python -m pytest -q app/tests/unit/test_config_validation.py app/tests/unit/test_deepstream_face_engine.py app/tests/unit/test_deepstream_mock_input.py app/tests/unit/test_deepstream_tracking.py app/tests/unit/test_evidence_store.py app/tests/unit/test_notifications.py app/tests/unit/test_safety_launcher.py
```

Khi test fail, chạy riêng node với output đầy đủ:

```powershell
& $python -m pytest -vv -s --capture=tee-sys '<file>::<test_node>' 2>&1 | Tee-Object '.tmp\pytest-failed-node.log'
```

Phân loại riêng unit, static, Compose config/build, container startup, dashboard health, MediaMTX health,
GPU/provider và evidence. Số test collected, exit code, process tồn tại hoặc launcher message không
tự động là acceptance.

## Real Compose E2E

E2E dùng mock profile nhưng chạy container, supervisor, worker, MediaMTX và HTTP API thật:

```powershell
& $python app\tests\e2e\run_camera_safety_e2e.py --duration 30 --wait 120 --report .tmp\ls-vision-e2e\final-summary.json
```

Nếu cần build trong E2E, dùng --build sau khi xác nhận Docker Desktop storage trên ổ E.
Acceptance chỉ hợp lệ khi report có accepted=true và toàn bộ gate sau đều true:

- Compose config/startup;
- dashboard live/ready;
- đúng một worker cho camera_face, camera_safety, DMS;
- cả ba camera ready và frame input/output fresh;
- MediaMTX HLS output;
- event feed chỉ có record START;
- container restart;
- state/evidence API còn hoạt động sau restart.

E2E mock không thay thế production acceptance với camera thật, model thật và GPU provider thật.
Giữ report/log/evidence của lần fail để điều tra; không overwrite hoặc xóa trước khi ghi nhận nguyên nhân.

## Live runtime và evidence

Sau production start:

```powershell
docker compose -f app\deploy\docker\compose.yaml ps
(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/health/live').StatusCode
(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/health/ready').StatusCode
Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/api/metrics'
docker compose -f app\deploy\docker\compose.yaml logs --no-color --tail 200 ls-vision
```

Kiểm tra MediaMTX bằng client/FFmpeg với RTSP 127.0.0.1:8554, HLS 127.0.0.1:8888 hoặc WebRTC
127.0.0.1:8889 theo output URL trong dashboard. Với camera_safety, xác nhận raw person boxes chỉ
hiện sau smoking confirmation, label fire/smoke dùng format canonical và temporal smoothing không
flicker theo từng inference cycle.

Evidence phải được kiểm tra từ run đang hoạt động và named volume tương ứng: manifest, event records,
SQLite idempotency/state, trace, thumbnail/original và notification outbox. Không đọc từng event.json
hoặc glob toàn bộ evidence tree trong dashboard request path.

Không dùng docker compose down -v hoặc xóa named volumes nếu chưa được yêu cầu rõ ràng; lệnh đó có thể
xóa SQLite/state/evidence. Không xóa evidence khi worker đang chạy.

## Git và bàn giao

Trước commit/push:

```powershell
git status --short
git diff --check
git diff --stat
```

Stage bằng các path đã xác nhận, không dùng git add ., git add -A hoặc git add --all.
Sau commit/push phải xác nhận git status --short sạch và local/remote branch đồng bộ.
