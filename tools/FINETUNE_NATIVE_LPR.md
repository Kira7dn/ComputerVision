# Fine-tune native LPR camera 1

Dataset labels must use YOLO format and this class order:

```text
0 car
1 license_plate
```

The extractor creates images and an annotation guide, but intentionally does
not create labels. After annotation, run with the PyTorch weights corresponding
to the native YOLOv8n model:

```powershell
python tools/train_native_lpr.py `
  --weights D:\path\to\native-yolov8n.pt `
  --data-root .tmp\lpr-dataset-v2 `
  --output .tmp\models\native-lpr-finetuned `
  --epochs 100 --batch 16 --imgsz 640 --device 0
```

The script validates all three splits, trains, evaluates on `test`, and exports
`native-lpr-finetuned.onnx` with static `640x640`, batch 1, no NMS insertion and
no score multiplier. It never changes the generated runtime config or active model.
