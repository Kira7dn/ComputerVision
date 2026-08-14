# Test layout

| Folder | Scope | How to run |
| --- | --- | --- |
| `unit/` | Pure scorer, manifest, assignment, funnel and report helper tests; no Docker/runtime required | `python -m pytest tools/tests/unit -q` |
| `integration/` | Cross-component integration checks; package-owned Frigate checks remain in `frigate/tests/` | `python -m pytest frigate/tests/test_evidence_quality.py frigate/tests/test_recognition_lifecycle.py -q` |
| `e2e/` | Default tracker → Frigate → recognition healthy Docker E2E with direct MP4 inputs | `python tools/tests/e2e/run_platform_runtime_test.py` |

Default E2E force-recreates its test services with run-scoped config, database, TLS and evidence,
then restores the configured runtime after artifact collection. It does not tear down the Compose
network just to recreate the same services. Current source is bind-mounted into Frigate, external
recognition and tracker, so development verification remains independent from an image build.
Successful tracker runs retain only report data, final clips/traces, debug images, compact SQLite
evidence and container/launcher logs. Transport I420, raw edge staging, test TLS and SQLite
sidecars are removed only after acceptance passes; failed runs preserve them for diagnosis.

`deploy/run.ps1 dev-start` additionally mounts the same source into external recognition and
starts Docker Compose watch for every Python service. Saving source restarts the affected
container without rebuilding an image. The E2E entrypoint itself validates Docker/configuration,
creates missing services with `--no-build`, and waits for recognition, tracker and Frigate
readiness before replay input starts.

Report aggregation is kept separately in `tools/reporting/`:

```powershell
python tools/reporting/summarize_platform_runtime.py `
  .tmp/platform-runtime-1/summary.json `
  .tmp/platform-runtime-2/summary.json `
  .tmp/platform-runtime-3/summary.json `
  --output .tmp/platform-runtime-evidence-report.json
```
