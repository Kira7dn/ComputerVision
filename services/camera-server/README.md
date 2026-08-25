# Camera Server

Camera Server là service độc lập cho Dahua ADAS, FTP ingest, archive và media/control API.
Service này được giữ để tái sử dụng nhưng không thuộc startup path, media ownership hay
production acceptance của LS-Vision.

## Layout

```text
camera_server/  # Python package và production entrypoint
config/         # Environment template
models/         # Model mặc định do Camera Server sở hữu
runtime/        # Upload, model generated, TLS và local state ngoài Git
tests/          # Unit tests của service
typings/        # Local type stubs
vendor/         # SDK boundary; binary SDK được giữ ngoài Git
```

## Development

Từ workspace root:

```powershell
$python = '.\.venv\Scripts\python.exe'
& $python -m pytest -c services/camera-server/pyproject.toml services/camera-server/tests -q
Push-Location services\camera-server
& ..\..\.venv\Scripts\python.exe -m ruff check camera_server tests
Pop-Location
```

Chạy service từ đúng working directory để package và path runtime không phụ thuộc root:

```powershell
Set-Location services\camera-server
& ..\..\.venv\Scripts\python.exe -m camera_server.main
```

Copy `config/.env.example` thành secret file ngoài Git hoặc inject các biến môi trường.
Không ghi credential, runtime uploads, queue database, SDK binary hoặc model generated vào source.

Các probe phần cứng chạy thủ công nằm dưới `camera_server/tools/`; chúng không phải pytest test.
