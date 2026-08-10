# Camera tooling map

Root `tools/` chỉ giữ package marker. Mọi tool được phân loại theo công dụng:

| Folder | Nội dung |
| --- | --- |
| `runtime/` | Validator runtime và các validator chuyên biệt/legacy |
| `tests/unit/` | Unit tests không cần Docker |
| `tests/integration/` | Quy ước và điểm vào integration test |
| `tests/e2e/` | Runtime replay test thực tế |
| `reporting/` | Tổng hợp và xuất report |
| `fixtures/` | Manifest và fixture builder |
| `annotations/` | Export/import annotation thủ công |
| `model/` | Dataset, A/B test, training và model validation |
| `lib/` | Helper dùng chung, không phải entrypoint |

## Entry point chuẩn

```powershell
python tools/tests/e2e/run_platform_runtime_test.py
python -m pytest tools/tests/unit -q
python tools/reporting/summarize_platform_runtime.py <run1> <run2> <run3> --output <report>
```

Mỗi lần chạy tự ghi vào `.tmp/platform-runtime/<timestamp>/`; không truyền output flag và không
ghi đè run trước. Các tên cũ trong `runtime/` và `reporting/` chỉ giữ để tương thích với artifact
hoặc script lịch sử; không dùng làm entrypoint mới.
