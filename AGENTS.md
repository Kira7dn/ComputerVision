# Camera workspace runbook

## Current architecture boundary

The current Camera runtime is the WSL-hosted DeepStream stack under `deepstream_safety/`:

`config.yaml` -> `multi_runner.py` -> one `pipeline.py` worker per camera -> MediaMTX RTSP output
and `dashboard_server.py` on `http://localhost:8080`.

Camera functions are selected from `deepstream_safety/config.yaml` per camera. The current
functions include face recognition, smoking behavior, fire/smoke detection, and trace/evidence.
`EvidenceStore` owns run evidence under `.tmp/deepstream-safety`.

The nested `frigate/` tree and the old Docker/Frigate tracker architecture are not part of the
current Camera runtime. Do not use them as a startup path, test gate, media owner, event store, or
source of truth for DeepStream changes. Do not add Camera guidance that routes media through
Frigate, `/media/frigate`, Frigate APIs, or `deploy/run.ps1`.

## Shell and encoding

Run commands from PowerShell. Always read Vietnamese text as UTF-8.

```powershell
$cameraRoot = 'D:\BusinessAnalyze\Camera'
$python = Join-Path $cameraRoot '.venv\Scripts\python.exe'
Set-Location $cameraRoot
Get-Content -LiteralPath <path> -Encoding utf8
```

Use the shared root interpreter. Do not create a nested virtual environment.

## Preflight

```powershell
git status --short
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -in @('python.exe', 'pytest.exe') -and $_.CommandLine -like '*BusinessAnalyze\Camera*' } |
  Select-Object ProcessId, Name, CommandLine
wsl.exe -d Ubuntu-22.04 -- bash -lc "pgrep -af '[m]ediamtx|[d]ashboard_server.py|[m]ulti_runner.py|[p]ipeline.py|[f]fmpeg.*mock' || true"
```

After an interrupted run, stop only confirmed stale Camera processes. Keep failed evidence for
diagnosis until it is no longer needed. When cleaning `.tmp`, stop the runtime first and target
only `D:\BusinessAnalyze\Camera\.tmp`.

## Configuration and runtime ownership

- `deepstream_safety/config.yaml` is the source of truth for camera IDs, sources, outputs, and
  enabled functions.
- `deepstream_safety/multi_runner.py` creates one worker per configured camera and one shared run ID.
- `deepstream_safety/pipeline.py` owns inference, tracking/annotation, RTSP output, and function
  dispatch for one camera.
- `deepstream_safety/start.ps1` is the WSL launcher for MediaMTX, dashboard, mock inputs, and all
  DeepStream workers.
- `package.json` exposes `npm run camera:start`, `npm run camera:stop`, and
  `npm run camera:status`.
- `deepstream_safety/dashboard_server.py` serves the dashboard at `http://localhost:8080`.
- `.tmp/deepstream-safety/snapshots-acceptance-<run-id>` contains the manifest, SQLite idempotency
  index, event records, traces, and accepted snapshots for a run.

## Starting and stopping the runtime

Preferred commands:

```powershell
npm run camera:start
npm run camera:status
npm run camera:stop
```

Equivalent direct launcher commands:

```powershell
.\deepstream_safety\start.ps1 start
.\deepstream_safety\start.ps1 status
.\deepstream_safety\start.ps1 stop
```

`camera:start` starts WSL services and the configured `camera_face` and `camera_safety` workers in
the foreground. The terminal remains attached to the worker log and appends the same output to
`/opt/camera-deepstream/logs/pipeline.log`. `Ctrl+C` ends the foreground client; use
`npm run camera:stop` to terminate the WSL services reliably. A successful launcher message is not
sufficient proof of health; verify the process list, dashboard HTTP 200, RTSP output, and recent
pipeline log activity.

Do not start individual workers in parallel with `multi_runner.py` unless isolating a failure.
Do not use Docker compose, `deploy/run.ps1`, or Frigate services for this runtime.

## Targeted tests

Run only the checks relevant to the change:

```powershell
& $python -u -m pytest -q `
  tools/tests/unit/test_deepstream_face_engine.py `
  tools/tests/unit/test_evidence_store.py `
  tools/tests/unit/test_safety_launcher.py
```

For a failed test, rerun only its file or node with verbose output:

```powershell
& $python -u -m pytest -vv -s --capture=tee-sys `
  '<file>::<test_node>' 2>&1 | Tee-Object '.tmp\pytest-failed-node.log'
```

## Static checks

```powershell
& $python -m ruff check deepstream_safety tools/tests
& $python -m compileall -q deepstream_safety

$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path 'deepstream_safety\start.ps1'),
  [ref]$null,
  [ref]$parseErrors
) > $null
$parseErrors

Get-Content -LiteralPath 'package.json' -Raw -Encoding utf8 | ConvertFrom-Json > $null
git diff --check
```

Classify unit tests, static checks, runtime startup, dashboard health, RTSP health, and evidence
inspection separately. A launcher message, process existence, timeout, or collected test count is
not by itself acceptance evidence.

## Live runtime verification

After starting the runtime:

```powershell
(Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8080/dashboard.html').StatusCode
wsl.exe -d Ubuntu-22.04 -- bash -lc "pgrep -af '[m]ediamtx|[d]ashboard_server.py|[m]ulti_runner.py|[p]ipeline.py|[f]fmpeg.*mock' || true"
Get-ChildItem '.tmp\deepstream-safety' -Directory |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1
```

Inspect the actual RTSP output with a client or FFmpeg. For `camera_safety`, verify that raw
person boxes are hidden unless smoking behavior is confirmed, fire/smoke labels use the canonical
`FIRE xx%` / `SMOKE AREA xx%` form, and temporal smoothing prevents inference-cycle flicker.

## Evidence and cleanup

Evidence is written only by the active DeepStream run. Inspect the newest run directory and its
`manifest.json`, `events.jsonl`, `index.sqlite3`, and camera/function event folders before calling
an acceptance run complete.

Do not delete evidence while a pipeline is writing it. To clean generated temporary artifacts:

```powershell
npm run camera:stop
# Inspect the exact target before removing D:\BusinessAnalyze\Camera\.tmp contents.
```

Preserve unrelated worktree changes. Do not use destructive Git commands such as `git reset --hard`,
`git checkout --`, or broad recursive deletion outside an explicitly approved temporary directory.
