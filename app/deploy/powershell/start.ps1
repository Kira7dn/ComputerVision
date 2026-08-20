param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',
    [ValidateSet('Dev', 'Production')]
    [string]$Mode = 'Production'
)

$ErrorActionPreference = 'Stop'
$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

if ($Mode -eq 'Production') {
    $compose = Join-Path $AppRoot 'deploy\docker\compose.yaml'
    switch ($Action) {
        'start' { docker compose -f $compose up -d --remove-orphans }
        'stop' { docker compose -f $compose down }
        'status' { docker compose -f $compose ps }
    }
    exit $LASTEXITCODE
}

$distro = 'Ubuntu-22.04'
$root = '/mnt/d/BusinessAnalyze/Camera'
$config = "$root/app/config/dev.yaml"
$runtime = '/opt/camera-safety-dev'

function Invoke-DevWsl([string]$Command) {
    & wsl.exe -d $distro --user root -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed: $Command" }
}

switch ($Action) {
    'start' {
        $command = @"
set -euo pipefail
export PYTHONPATH=$root/app/src
export CAMERA_CONFIG=$config
export NVDS_ENABLE_LATENCY_MEASUREMENT=1
export NVDS_ENABLE_COMPONENT_LATENCY_MEASUREMENT=1
mkdir -p $runtime/logs $runtime/status $runtime/state $runtime/evidence
if ! pgrep -f '[m]ediamtx.*camera-safety-dev' >/dev/null 2>&1; then
  nohup /opt/camera-safety/mediamtx/mediamtx $root/app/deploy/docker/mediamtx.yml >$runtime/logs/mediamtx.log 2>&1 &
fi
if ! pgrep -f 'camera_safety.interfaces.dashboard_api' >/dev/null 2>&1; then
  nohup python3 -m camera_safety.interfaces.dashboard_api >$runtime/logs/dashboard.log 2>&1 &
fi
if ! pgrep -f 'camera_safety.runner.*config/dev.yaml' >/dev/null 2>&1; then
  nohup python3 -m camera_safety.runner --config $config >$runtime/logs/pipeline.log 2>&1 &
fi
echo 'native WSL development runtime started'
"@
        Invoke-DevWsl ($command -replace "`r`n", "`n")
    }
    'stop' {
        Invoke-DevWsl "pkill -f 'camera_safety.runner' 2>/dev/null || true; pkill -f 'camera_safety.application.camera_worker' 2>/dev/null || true; pkill -f 'camera_safety.interfaces.dashboard_api' 2>/dev/null || true; pkill -f 'ffmpeg.*rtsp://127.0.0.1:8554/(face_mock|safety_mock)' 2>/dev/null || true"
        Write-Output 'native WSL development runtime stopped'
    }
    'status' {
        Invoke-DevWsl "pgrep -af '[m]ediamtx|camera_safety.runner|camera_safety.application.camera_worker|camera_safety.interfaces.dashboard_api|[f]fmpeg.*mock' || true; curl -fsS -o /dev/null -w 'dashboard_http=%{http_code}\n' http://127.0.0.1:18080/dashboard.html || true"
    }
}
