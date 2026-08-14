# Camera workspace runbook

## Shell and encoding

Run commands from PowerShell. Always read Vietnamese text as UTF-8.

```powershell
$cameraRoot = 'D:\BusinessAnalyze\Camera'
$python = Join-Path $cameraRoot '.venv\Scripts\python.exe'
Set-Location $cameraRoot
Get-Content -LiteralPath <path> -Encoding utf8
```

Use the shared root interpreter for workspace and nested `frigate`; do not create a nested virtual
environment.

## Preflight

```powershell
git status --short
git -C frigate status --short
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -in @('python.exe', 'pytest.exe') -and $_.CommandLine -like '*BusinessAnalyze\Camera*' } |
  Select-Object ProcessId, Name, CommandLine
```

After an interrupted test, stop only confirmed stale Camera processes. Remove stale test databases
with Python, not a recursive shell delete.

## Baseline commands

Upstream unittest discovery:

```powershell
Push-Location frigate
& $python -u -m unittest
Pop-Location
```

Frozen 248-test workspace baseline:

```powershell
Push-Location frigate
& $python -u -m pytest -c pytest-baseline.ini
Pop-Location
```

Run only the baseline gate requested for the task; do not automatically run both and do not replace
either command with broad `pytest frigate/tests` collection.

## Targeted tests

Nested Frigate files:

```powershell
Push-Location frigate
$env:PYTHONPATH = 'src'
& $python -u -m pytest -vv -s --capture=tee-sys -o log_cli=true -o log_cli_level=DEBUG `
  tests/test_tracker_edge.py `
  tests/test_notification_media.py `
  tests/test_notification_providers.py `
  tests/test_ptz_autotrack.py `
  2>&1 | Tee-Object '..\.tmp\pytest-frigate-targeted.log'
Remove-Item Env:PYTHONPATH
Pop-Location
```

Workspace launcher and runtime validator tests:

```powershell
& $python -u -m pytest -vv -s --capture=tee-sys -o log_cli=true -o log_cli_level=DEBUG `
  tools/tests/unit/test_external_tracker_launcher.py `
  tools/tests/unit/test_passage_acceptance.py `
  2>&1 | Tee-Object '.tmp\pytest-runtime-targeted.log'
```

When a test fails, rerun only its file or node with the same verbose/logging options:

```powershell
& $python -u -m pytest -vv -s --capture=tee-sys -o log_cli=true -o log_cli_level=DEBUG `
  '<file>::<test_node>' 2>&1 | Tee-Object '.tmp\pytest-failed-node.log'
```

## Static checks

```powershell
& $python -m ruff check server frigate/src tools
& $python -m ty check

$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'deploy\run.ps1'),
  [ref]$null,
  [ref]$parseErrors
) > $null
$parseErrors

git diff --check
```

Generate API or translations only from their source scripts:

```powershell
& $python frigate/generate_api_auth_spec.py
& $python frigate/generate_config_translations.py
```

Do not edit `frigate/docs/static/frigate-api.yaml` or generated translation artifacts manually.

## Development runtime

Read the launcher before selecting an action:

```powershell
Get-Content -LiteralPath 'deploy\run.ps1' -Encoding utf8
```

Start source-mounted development services with hot reload and no image build:

```powershell
.\deploy\run.ps1 dev-start
.\deploy\run.ps1 status
.\deploy\run.ps1 dev-logs
.\deploy\run.ps1 dev-restart
.\deploy\run.ps1 dev-stop
```

Build only when explicitly requested for a production/release check:

```powershell
.\deploy\run.ps1 build
```

## Official E2E commands

Default healthy tracker runtime:

```powershell
& $python -u tools/tests/e2e/run_platform_runtime_test.py `
  2>&1 | Tee-Object '.tmp\platform-runtime-e2e.log'
```

Healthy external-recognition runtime:

```powershell
& $python -u tools/tests/e2e/run_external_recognition_runtime_test.py `
  2>&1 | Tee-Object '.tmp\external-recognition-e2e.log'
```

`tools/runtime/validate_platform_runtime.py` is an implementation detail. Do not invoke it directly
and report that invocation as an official E2E entrypoint.

## E2E evidence checks

```powershell
$run = Get-ChildItem '.tmp\platform-runtime' -Directory |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1
$summary = Get-Content -LiteralPath (Join-Path $run.FullName 'summary.json') -Raw -Encoding utf8 |
  ConvertFrom-Json

$summary.accepted
$summary.acceptance.status
$summary.measurement.measurement_valid
$summary.gates.runtime_restored
$summary.timing

Get-ChildItem (Join-Path $run.FullName 'media') -Recurse -File -Filter 'clip.mp4'
Get-ChildItem (Join-Path $run.FullName 'media') -Recurse -File -Filter 'trace.json'
docker ps --format '{{.Names}}|{{.Status}}'
```

Classify baseline, targeted tests, static checks, build, healthy E2E and restore separately. A
collected test count, launcher scaffold, process timeout or incomplete report is not pass evidence.
Keep failed runtime artifacts unchanged for diagnosis.
