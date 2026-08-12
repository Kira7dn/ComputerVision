# Camera workspace instructions

## Encoding

- Khi đọc file có tiếng Việt, luôn đọc bằng UTF-8.
- Khi ghi hoặc sửa file có tiếng Việt, giữ UTF-8 và không làm hỏng dấu/cấu trúc hiện có.

## Execution gates

Mọi thay đổi runtime phải đi theo thứ tự bắt buộc sau:

1. Review source và contract ở root cùng nested `frigate`.
2. Chạy unit/regression test liên quan và toàn bộ test bắt buộc.
3. Chạy `compileall`, Ruff trên Python files đã đổi và `git diff --check`.
4. Chỉ build/package khi ba bước trên pass.
5. Kiểm tra clean-install/import và reproducibility; lưu source commit, worktree hash,
   artifact SHA-256 và byte size.
6. Mọi build, healthy run và fault run của Camera phải đi qua launcher dùng chung
   `deploy/run.ps1`; không gọi Docker Compose/container trực tiếp để thay thế launcher.
7. Chỉ chạy healthy Docker acceptance sau khi build pass.
8. Chỉ chạy fault scenarios sau khi healthy run pass.
9. Chỉ đánh dấu `[DONE]` khi toàn bộ hard gate, cleanup và restore artifact đều pass.

Nếu unit hoặc contract fail thì dừng, không build và không chạy Docker. Nếu build fail thì
dừng, không chạy Docker. Nếu healthy run fail thì không chạy fault scenarios. Nếu
`deploy/run.ps1` không được dùng hoặc launcher báo lỗi thì run không hợp lệ.

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
- Không gọi `validate_platform_runtime.py` trực tiếp rồi gọi đó là entrypoint E2E chính thức;
  khi báo cáo hoặc chạy healthy E2E phải dùng file trong `tools/tests/e2e`.

## Camera architecture constraints

- Không thay model, OCR, threshold, độ phân giải hoặc recognition algorithm để ép accuracy,
  trừ khi user mở rộng phạm vi rõ ràng.
- Giữ kết quả LPR khó đọc dưới dạng diagnostic; không dùng accuracy diagnostic làm lý do sửa
  pipeline hoặc đánh dấu giả là pass.
- Camera embedded tiếp tục chạy nguyên pipeline Frigate. Với camera được gán edge,
  tracker node sở hữu capture/detect/Norfair/PTZ/recording/live/media; không fork hoặc
  viết lại logic hành vi gốc để tạo implementation thứ hai.
- Frigate main tiếp tục là SOT duy nhất cho Event/API/SQLite/notification/publication,
  nhận media manifest và proxy byte-range từ edge; main không tính lại tracker decision.
- Recognition service sở hữu model, core và session state.
- Không local fallback trong external topology.
- Tracker không được giao tiếp trực tiếp với recognition.
- Giữ canonical Event SOT, producer Event ID, idempotency và no-duplicate contract.
- Mọi fault run phải ghi typed lifecycle outcome, service epoch, publication safety,
  pending/in-flight/queue, cleanup và topology restore.

## Verification reporting

- Phân biệt rõ source change, unit pass, build pass, healthy runtime pass và fault pass.
- Không kết luận pipeline hoàn tất chỉ từ unit test, build thành công hoặc một healthy run.
- Giữ artifact mới có thể truy vết về source commit/worktree hash.
