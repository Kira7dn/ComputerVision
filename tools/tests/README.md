# Test layout

| Folder | Scope | How to run |
| --- | --- | --- |
| `unit/` | Pure scorer, manifest, assignment, funnel and report helper tests; no Docker/runtime required | `python -m pytest tools/tests/unit -q` |
| `integration/` | Cross-component integration checks; package-owned Frigate checks remain in `frigate/tests/` | `python -m pytest frigate/tests/test_evidence_quality.py frigate/tests/test_recognition_lifecycle.py -q` |
| `e2e/` | Direct-MP4 LPR tracking plus real Docker/Frigate enrichment runtime | `python tools/tests/e2e/run_platform_runtime_test.py` |

Report aggregation is kept separately in `tools/reporting/`:

```powershell
python tools/reporting/summarize_platform_runtime.py `
  .tmp/platform-runtime-1/summary.json `
  .tmp/platform-runtime-2/summary.json `
  .tmp/platform-runtime-3/summary.json `
  --output .tmp/platform-runtime-evidence-report.json
```
