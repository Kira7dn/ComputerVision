# Camera tooling map

Root `tools/` chỉ giữ package marker. Mọi tool được phân loại theo công dụng:

| Folder | Nội dung |
| --- | --- |
| `runtime/` | Validator runtime và các validator chuyên biệt/legacy |
| `tests/unit/` | Legacy helper tests only; Camera Safety tests live under `app/tests/unit/` |
| `tests/integration/` | Legacy helper documentation only |
| `tests/e2e/` | Removed Frigate-specific runners; Camera Safety E2E lives under `app/tests/e2e/` |
| `reporting/` | Tổng hợp và xuất report |
| `fixtures/` | Manifest và fixture builder |
| `annotations/` | Export/import annotation thủ công |
| `model/` | Dataset, A/B test, training và model validation |
| `lib/` | Helper dùng chung, không phải entrypoint |

## Entry point chuẩn

```powershell
python app/tests/e2e/run_camera_safety_e2e.py
python -m pytest app/tests -q
```

Mỗi lần chạy tự ghi vào `.tmp/platform-runtime/<timestamp>/`; không truyền output flag và không
ghi đè run trước. Các tên cũ trong `runtime/` và `reporting/` chỉ giữ để tương thích với artifact
hoặc script lịch sử; không dùng làm entrypoint mới.
