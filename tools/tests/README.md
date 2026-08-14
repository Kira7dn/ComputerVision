# Test layout

| Folder | Scope | How to run |
| --- | --- | --- |
| `unit/` | Pure scorer, manifest, assignment, funnel and report helper tests; no Docker/runtime required | `python -m pytest tools/tests/unit -q` |
| `integration/` | Cross-component integration checks; package-owned Frigate checks remain in `frigate/tests/` | `python -m pytest frigate/tests/test_evidence_quality.py frigate/tests/test_recognition_lifecycle.py -q` |
| `e2e/` | Default tracker → Frigate → recognition healthy Docker E2E with direct MP4 inputs | `python tools/tests/e2e/run_platform_runtime_test.py` |

Default E2E uses an isolated stop/start/restore lifecycle and bind-mounts the current source into
Frigate and tracker. This keeps development verification independent from an image build while
restoring the configured runtime after artifact collection.

Report aggregation is kept separately in `tools/reporting/`:

```powershell
python tools/reporting/summarize_platform_runtime.py `
  .tmp/platform-runtime-1/summary.json `
  .tmp/platform-runtime-2/summary.json `
  .tmp/platform-runtime-3/summary.json `
  --output .tmp/platform-runtime-evidence-report.json
```
