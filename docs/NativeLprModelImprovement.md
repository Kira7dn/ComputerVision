# Kế hoạch cải thiện model native LPR camera 1

## Model active và candidate

Model active vẫn là `models/roboflow-logistics-yolov8/best-frigate.onnx` qua
`deploy/docker-compose.yml`. Không có model active nào bị ghi đè.

| Model | SHA-256 | Ghi chú |
|---|---|---|
| `best.onnx` | `a3d05eb85fb036ba8f4394d4bf55008e7b902c14fd9c7cdef1f27df89bee5fff` | output nguyên bản |
| `best-frigate.onnx` | `d004eacc90814c733e956b0828ff335d89566c229c32d293c4beff4802e03b84` | cùng hash với `logistics-yolov8-frigate-plategain225.onnx`, có plate score gain |

Cả hai model có cùng input `1x3x640x640`, output `1x24x8400`, class `license
plate` là class 9 và `car` là class 1. `best-frigate.onnx` có output plate
score thô vượt `1.0` (`1.0264` trong mẫu kiểm tra); `best.onnx` không vượt `1.0`.

## Đo A/B hiện tại

Đo bằng `tools/lpr_ab_test.py` trên cùng 10 frame cách đều của video camera 1,
threshold model-only `0.2`:

| Model | Frame có plate | Plate boxes thô | Score > 1 | Duplicate IoU >= 0.8 |
|---|---:|---:|---:|---:|
| `best.onnx` | 4/10 | 18 | 0 | 38 |
| `best-frigate.onnx` | 10/10 | 66 | 0 trong mẫu threshold 0.2 | 116 |

Đây là output trước NMS của ONNX, nên duplicate chưa phải kết luận rằng
Frigate event cuối cùng có duplicate. Nó là tín hiệu cần kiểm tra post-processing.
Ở threshold `0.5`, mẫu 20 frame ghi nhận `best.onnx` không có plate box nào,
trong khi `best-frigate.onnx` có 12/20 frame có plate và một score `>1`.

Kết luận hiện tại: chưa có bằng chứng để thay active model bằng `best.onnx`;
model nguyên bản cần threshold/candidate test riêng và chưa có OCR E2E tương ứng.

## Dữ liệu fine-tune

Đã trích 120 frame từ:

`mock_videos/car-number-plate-video/cam-in/Traffic Control CCTV.mp4`

Artifact nằm ngoài Git tại `.tmp/lpr-dataset-v2` và được chia theo block thời
gian thành `72 train / 24 val / 24 test`. Cần annotate cả `car` và
`license_plate`, ưu tiên plate nhỏ, blur, nghiêng, sát mép và tương phản thấp.

Chưa fine-tune vì repo chưa có label cho các frame này và không có file PyTorch
weights tương ứng với native logistics model. Không dùng pseudo-label hoặc model
dedicated LPR để tránh làm sai phép đo native `car + license_plate`.

## Cách chạy lại

```powershell
python tools/lpr_ab_test.py --samples 20 --threshold 0.5
python tools/lpr_ab_test.py --samples 10 --threshold 0.2 --output .tmp/model-review/lpr-ab-threshold-0.2.json
python tools/extract_lpr_dataset.py --output .tmp/lpr-dataset-v2 --block-frames 180
```
