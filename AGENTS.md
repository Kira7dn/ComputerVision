# Camera workspace instructions

## Test scope

- Baseline regression của nested `frigate` phải bám đúng contract CI của upstream/master:
  chạy `python -m unittest` từ thư mục `frigate` (có thể dùng Python trong `.venv` của
  workspace). Không tự biến toàn bộ `frigate/tests` thành một pytest gate mới.
- Sau baseline, chỉ chạy test unit/contract trực tiếp bao phủ boundary bị thay đổi. Với Phase 8,
  tập test chuẩn gồm:
  - tracker contract, lifecycle, evidence, journal, media và ownership:
    `frigate/tests/test_tracker_edge.py`;
  - notification dùng edge media:
    `frigate/tests/test_notification_media.py` và
    `frigate/tests/test_notification_providers.py`;
  - PTZ wrapper/runtime: `frigate/tests/test_ptz_autotrack.py`;
  - launcher và acceptance validator:
    `tools/tests/unit/test_external_tracker_launcher.py` cùng các test node liên quan trực tiếp
    trong `tools/tests/unit/test_passage_acceptance.py`.
- Khi một test fail, chẩn đoán và chạy lại đúng test file/test node bị fail. Không chạy lại suite
  rộng nếu thay đổi sửa lỗi không tác động tới phần còn lại.
- Chỉ mở rộng test ngoài danh sách trên khi diff thực sự chạm boundary khác, hoặc user/spec yêu
  cầu rõ ràng; phải nêu chính xác lý do và test được thêm trước khi chạy.
- Sau khi baseline và targeted architecture tests pass, mới build qua `deploy/run.ps1`, rồi chạy
  healthy E2E chính thức; fault E2E chỉ chạy sau healthy pass.
- Báo cáo riêng từng trạng thái: baseline regression, targeted architecture tests, build,
  healthy E2E và fault E2E. Không dùng số test được collect làm bằng chứng pass.

## Launcher contract

- Trước khi chạy phải đọc lại `deploy/run.ps1` và chọn đúng action/config/topology.
- Launcher phải kiểm tra readiness bounded trước khi Frigate bắt đầu acceptance.
- Artifact phải ghi command/launcher, config hash, source/worktree hash, topology và restore result.
- Khi launcher hoặc runtime fail, giữ nguyên artifact lỗi để chẩn đoán; không sửa report thủ công
  để biến run thành pass.

## E2E entrypoints

- Healthy external E2E chính thức là:
  `tools/tests/e2e/run_external_recognition_runtime_test.py`.
- Runner/validator được entrypoint healthy gọi là:
  `tools/runtime/validate_platform_runtime.py`.
- Fault E2E chính thức là:
  `tools/tests/e2e/run_external_recognition_fault_test.py` với các scenario
  `service_restart`, `stream_disconnect`, `client_disconnect`.
- Healthy tracker-edge E2E entrypoint là:
  `tools/tests/e2e/run_external_tracker_runtime_test.py`.
- Fault tracker-edge E2E entrypoint là:
  `tools/tests/e2e/run_external_tracker_fault_test.py` với các scenario
  `tracker_restart`, `stream_disconnect`, `client_disconnect`, `spool_replay`,
  `media_unavailable`.
- Tracker entrypoint chỉ được báo `pass` khi healthy runner validate đủ tracker → Frigate main →
  recognition → Event/API/SQLite/media và fault runner thực thi scenario qua action của
  `deploy/run.ps1`. File scaffold hoặc chỉ gọi `acceptance-start` không phải bằng chứng E2E pass.
- Không gọi `validate_platform_runtime.py` trực tiếp rồi gọi đó là entrypoint E2E chính thức;
  khi báo cáo hoặc chạy healthy E2E phải dùng file trong `tools/tests/e2e`.

## Nested Frigate development standards

- Nested Frigate production code lives under `frigate/src`; the workspace extension lives under
  `frigate/src/camera_platform`.
- The embedded Frigate tracking core lives under `frigate/src/frigate/domain/track`; edge tracker
  transport, ownership, journal, media, and topology live under `frigate/src/camera_platform/tracker`.
- API routes remain under `frigate/src/frigate/api`; application orchestration remains under
  `frigate/src/frigate/application`; system adapters remain under `frigate/src/frigate/infrastructure`.
- Use the shared root environment `D:\BusinessAnalyze\Camera\.venv` for both workspace and nested
  Frigate commands. The nested repository has no private virtual environment.
- Python code uses module-level loggers, lazy logging, specific exceptions, and non-blocking async
  I/O. Log messages contain no credentials or tokens and do not end with a period.
- Frontend user-facing text uses the existing i18n system. Generated API and translation artifacts
  are regenerated from their source and are not edited manually.
- AI-assisted changes require human review, local understanding, explicit disclosure in external
  contribution material, and evidence based on observed output rather than unsupported diagnosis.

## Test diagnostics and process cleanup

- Mặc định mọi lần chạy pytest dùng verbose với log realtime:
  `python -u -m pytest -vv -s --capture=tee-sys -o log_cli=true -o log_cli_level=DEBUG`.
- Mặc định lưu toàn bộ output bằng PowerShell `Tee-Object`, ví dụ:
  `... 2>&1 | Tee-Object .tmp\pytest-test.log`.
- Các lần chạy sau interrupt bắt đầu bằng việc kiểm tra process `python.exe`/`pytest` của
  Camera, dừng process stale và dọn `test.db`, `test.db-shm`, `test.db-wal` bằng Python.
- Mỗi node hoặc file fail được chạy riêng với log đầy đủ; phạm vi rerun bám đúng failure.
- Process thiếu CPU progress hoặc log trong thời gian bất thường được dừng lại; log lỗi được
  giữ nguyên và node kế tiếp được cô lập để chẩn đoán.
## Nested Frigate development standards

- Nested Frigate production code lives under `frigate/src`; the workspace extension lives under
  `frigate/src/camera_platform`.
- Embedded Frigate tracking core lives under `frigate/src/frigate/domain/track`.
  Edge tracker transport, ownership, journal, media, and topology live under
  `frigate/src/camera_platform/tracker`.
- API routes live under `frigate/src/frigate/api`.
- Application orchestration lives under `frigate/src/frigate/application`.
- Domain behavior lives under `frigate/src/frigate/domain`.
- System adapters and external integrations live under `frigate/src/frigate/infrastructure`.
- Shared utilities live under `frigate/src/frigate/util`.
- Use `D:\BusinessAnalyze\Camera\.venv` for all workspace and nested Frigate commands.
  The nested repository has no private virtual environment.
- Workspace Python compatibility is Python 3.11, as configured in the root `pyproject.toml`.
- Python code uses module-level loggers, lazy logging, specific exceptions, and non-blocking async
  I/O. Log messages contain no credentials or tokens and do not end with a period.
- Frontend user-facing text uses the existing i18n system. Generated API and translation artifacts
  are regenerated from their source and are not edited manually.
- AI-assisted changes require human review, local understanding, explicit disclosure in external
  contribution material, and evidence based on observed output rather than unsupported diagnosis.

## Frigate commands and file ownership

- Run the 248-test upstream baseline from `D:\BusinessAnalyze\Camera\frigate`:
  `D:\BusinessAnalyze\Camera\.venv\Scripts\python.exe -u -m pytest -c pytest-baseline.ini`.
- Run one baseline test file with the same root interpreter and `PYTHONPATH=src`.
- Run targeted platform tests only after baseline, using files under `frigate/tests` and
  `tools/tests/unit` that cover the changed boundary.
- API and translation generators remain repository-root scripts:
  `frigate/generate_api_auth_spec.py` and `frigate/generate_config_translations.py`.
- Generated OpenAPI output is `frigate/docs/static/frigate-api.yaml`; never edit it manually.
- Frigate local configuration and state live under `frigate/config` and are ignored by Git.
- Frigate migrations live under `frigate/migrations`.
- Frigate Docker definitions live under `frigate/docker`.
- Frigate frontend lives under `frigate/web`.
- Run type checking from the workspace root with:
  `D:\BusinessAnalyze\Camera\.venv\Scripts\python.exe -m ty check`.
- Run Ruff using the root `pyproject.toml`; package paths are `server`, `frigate/src`, and `tools`.

## Frigate code conventions

- Public Python functions and methods have concise docstrings.
- Comments explain why behavior exists, not only what the code does.
- External I/O in async functions uses async clients and does not block the event loop.
- WebSocket outbound topics are classified by `frigate/src/frigate/infrastructure/comms/ws.py`.
- Configuration access uses the application config object and typed config models under
  `frigate/src/frigate/infrastructure/config`.
