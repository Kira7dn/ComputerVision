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
