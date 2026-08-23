# Local fire/smoke P0 training

Pipeline này tạo candidate ở `.tmp/`, không ghi đè model runtime trong
`assets/models/fire_smoke/`.

## Chuẩn bị môi trường

Chạy từ `D:\BusinessAnalyze\Camera` và dùng Python 3.10/3.11 có PyTorch phù hợp
với GPU local. Cài PyTorch CUDA theo phiên bản CUDA của máy, sau đó:

```powershell
$python = '.venv\Scripts\python.exe'
& $python -m pip install -r app\requirements-training.txt
```

## Dataset local

Script nhận dataset YOLO detection gồm đúng hai class `fire`, `smoke`:

```text
.tmp/fire-smoke-fixture-v2/
├── data.yaml
├── images/{train,val,test}/*.jpg
└── labels/{train,val,test}/*.txt
```

`data.yaml` tối thiểu:

```yaml
path: D:/BusinessAnalyze/Camera/.tmp/fire-smoke-fixture-v2
train: images/train
val: images/val
test: images/test
names: [fire, smoke]
```

Để trộn dữ liệu public đã tải (CCTV nội bộ là tùy chọn cho exploratory candidate), mỗi source phải có `data.yaml`
cùng `images/{train,val,test}` và `labels/{train,val,test}`. Ghi metadata đã
pin vào catalog UTF-8 (không ghi secret):

```yaml
sources:
  - id: dfire-v1
    version: "<pinned release/version>"
    license: "<license from the release>"
    url: "<source URL>"
  - id: internal-cctv-v1
    version: "<annotation revision>"
    license: "workspace-internal"
    domain: "internal-cctv"
```

Chuẩn hóa và checksum source:

```powershell
& $python app\tools\prepare_fire_smoke_dataset.py `
  --source-yolo dfire-v1=.tmp\public\dfire `
  --source-catalog .tmp\fire-smoke-sources.yaml `
  --output .tmp\fire-smoke-dataset-p0
```

Nếu có CCTV nội bộ đã gán nhãn, thêm source đó vào lệnh trên. Không có CCTV nội bộ
vẫn có thể tạo candidate exploratory từ D-Fire + fixture; candidate này không được
coi là đã chứng minh độ chính xác theo domain camera thực tế.

Có thể tạo fixture dataset để kiểm tra pipeline:

```powershell
& $python app\tools\prepare_fire_smoke_dataset.py `
  --output .tmp\fire-smoke-fixture-v2
```

Dataset public phải được tải và chuẩn hóa thủ công về format trên trước khi train;
không tự động scrape URL vì D-Fire/FASDD có format, version và điều khoản khác nhau.
Dữ liệu public dùng cho pretrain/exploratory; fixture dùng để regression. Nếu có CCTV
nội bộ, giữ split theo camera/video hoặc scene để tránh rò rỉ các frame liền kề và dùng
nó làm domain evaluation. Không có CCTV nội bộ thì không thể kết luận false alarms/hour
theo domain thực tế.

## Training candidate

```powershell
& $python app\tools\train_fire_smoke.py `
  --data .tmp\fire-smoke-fixture-v2\data.yaml `
  --weights assets\models\fire_smoke\best.pt `
  --project .tmp\fire-smoke-training `
  --name candidate-p0-v1 `
  --epochs 80 `
  --batch 4 `
  --device 0
```

Script sẽ kiểm tra class/split/label/normalized box trước khi gọi Ultralytics,
train `best.pt`, validate trên `test`, export ONNX opset 17 và ghi
`candidate-report.json`. Nếu chạy CPU để chẩn đoán, thêm `--allow-cpu`;
không dùng kết quả CPU làm acceptance latency.

## Kết quả run thực tế — 2026-08-22

Đã chạy GPU bằng D-Fire đã tải và chuẩn hóa. Vì workspace không có CCTV nội bộ, run này là
exploratory public-plus-fixture; không được xem là bằng chứng đủ để promote theo domain camera.

- Dataset: D-Fire train/val/test lần lượt `14,122 / 3,099 / 4,306` ảnh. Manifest nguồn là
  `.tmp/fire-smoke-dfire-sources.yaml`; checksum archive và thống kê chuẩn hóa nằm trong
  `E:\Camera-fire-smoke-p0\workspace\fire-smoke-dataset-dfire-v6\dataset-report.json`.
- CUDA: NVIDIA GeForce RTX 3050 Laptop GPU, PyTorch `2.11.0+cu128`, CUDA `12.8`, device `0`.
- Run: `3` epochs, batch `8`, `imgsz=640`, candidate
  `E:\Camera-fire-smoke-p0\workspace\fire-smoke-training\dfire-gpu-p0-v3\`.
- Kết quả D-Fire test: precision `58.4%`, recall `53.7%`, mAP50 `56.2%`; P95 ONNX CUDA
  `14.08 ms`, output shape `[1, 6, 8400]`.
- Theo class trên validation: fire precision/recall `57.3%/51.7%`; smoke
  `59.6%/55.7%`.
- Report đầy đủ: `E:\Camera-fire-smoke-p0\workspace\fire-smoke-training\dfire-gpu-p0-v3\candidate-report.json`.
- Candidate đã được đăng ký gần model production dưới version
  `assets/models/fire_smoke/versions/v2-dfire-gpu-20260822/`, gồm `best.pt`, `best.onnx`, report
  và `version-manifest.json`. Đây vẫn là candidate, pipeline chưa chuyển sang version này.
- Trạng thái: `accepted=false`. Chưa có same-test baseline, false alarms/hour, runtime
  parity đầy đủ và canary 8 giờ. FASDD chưa được pin/tải nên chưa dùng trong run.
- Production `assets/models/fire_smoke/best.pt` và `best.onnx` vẫn giữ nguyên; candidate chỉ
  nằm trong `.tmp/` và chưa đưa vào config hoặc canary.

## Promotion gate

Không copy candidate vào runtime chỉ vì training thành công. Cần kiểm tra trong report
và replay:

- smoke/fire recall và precision theo từng camera;
- false alarms/hour, đặc biệt smoke;
- P95 inference latency trên GPU runtime;
- fixture replay và RTSP thật với threshold hiện tại;
- ONNX/TensorRT parity và model checksum;
- event lifecycle không tạo duplicate hoặc flicker.

Candidate public-only có thể train và benchmark offline, nhưng chỉ sau các gate runtime
trên mới được canary; thiếu false alarms/hour hoặc domain evidence thì giữ
`accepted=false` và không thay đổi `app/config/*` hoặc manifest model.

Tạo config canary cho riêng `camera_safety`; các camera khác vẫn đọc baseline:

```powershell
& $python app\tools\prepare_fire_smoke_canary.py `
  --candidate .tmp\fire-smoke-training\candidate-p0-v1\weights\best.onnx `
  --output .tmp\fire-smoke-canary.yaml
.\app\deploy\powershell\start.ps1 -Mode Dev -Config .tmp\fire-smoke-canary.yaml
```

Launcher mặc định vẫn dùng `config\dev.yaml`. Canary report phải có tối thiểu 8
giờ, provider GPU active, không stale/out-of-order/duplicate, controlled fire
latency không quá 3 giây và số giờ negative CCTV đã label để tính
`false_alarms/hour`; thiếu bất kỳ gate nào thì giữ `accepted=false` và rollback
bằng cách stop canary rồi chạy lại config baseline.

Thu thập report canary (lệnh này không tự bơm controlled fire và không tự suy ra
false alarm từ event feed):

```powershell
& $python app\tools\monitor_fire_smoke_canary.py `
  --duration-hours 8 `
  --controlled-fire-report .tmp\controlled-fire-result.json `
  --report .tmp\fire-smoke-canary\report.json
```
