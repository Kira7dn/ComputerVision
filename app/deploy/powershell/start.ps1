param(
    [ValidateSet('start', 'pause', 'stop', 'status', 'logs')]
    [string]$Action = 'start',
    [ValidateSet('Dev', 'Production')]
    [string]$Mode = 'Production',
    [switch]$FollowLogs
)

$ErrorActionPreference = 'Stop'
$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ViteRoot = Join-Path $AppRoot 'web'
$VitePort = 5173
$VitePidFile = '\\wsl.localhost\Ubuntu-22.04\opt\camera-safety-dev\status\vite.pid'
$ViteStdout = '\\wsl.localhost\Ubuntu-22.04\opt\camera-safety-dev\logs\vite.stdout.log'
$ViteStderr = '\\wsl.localhost\Ubuntu-22.04\opt\camera-safety-dev\logs\vite.stderr.log'
$HotReloadPidPath = '/opt/camera-safety-dev/status/hot-reload.pid'

function Start-ViteDev {
    $existingPid = $null
    if (Test-Path -LiteralPath $VitePidFile) {
        $rawPid = (Get-Content -LiteralPath $VitePidFile -Raw -Encoding utf8).Trim()
        if ($rawPid -match '^\d+$') { $existingPid = [int]$rawPid }
    }
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) { return }
    if (Get-NetTCPConnection -LocalPort $VitePort -State Listen -ErrorAction SilentlyContinue) {
        throw "Vite port $VitePort is already owned by an untracked process"
    }
    $node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
    $viteScript = Join-Path $ViteRoot 'node_modules\vite\bin\vite.js'
    if (-not $node -or -not (Test-Path -LiteralPath $viteScript)) {
        throw 'Vite dependencies are not installed; run npm install in app\web first'
    }
    $viteProcess = Start-Process -FilePath $node -WorkingDirectory $ViteRoot -ArgumentList @($viteScript, '--host', '0.0.0.0', '--port', $VitePort) -RedirectStandardOutput $ViteStdout -RedirectStandardError $ViteStderr -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $VitePidFile -Value $viteProcess.Id -Encoding ascii
}

function Stop-ViteDev {
    if (-not (Test-Path -LiteralPath $VitePidFile)) { return }
    $pidContent = Get-Content -LiteralPath $VitePidFile -Raw -Encoding utf8 -ErrorAction SilentlyContinue
    if ($null -ne $pidContent) {
        $rawPid = $pidContent.Trim()
        if ($rawPid -match '^\d+$') {
            Stop-Process -Id ([int]$rawPid) -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $VitePidFile -Force -ErrorAction SilentlyContinue
}

function Stop-WslPidFile([string]$LinuxPidPath) {
    $uncPath = '\\wsl.localhost\Ubuntu-22.04' + ($LinuxPidPath -replace '/', '\')
    if (-not (Test-Path -LiteralPath $uncPath)) { return }
    $pidContent = Get-Content -LiteralPath $uncPath -Raw -Encoding utf8 -ErrorAction SilentlyContinue
    if ($null -ne $pidContent) {
        $rawPid = $pidContent.Trim()
        if ($rawPid -match '^\d+$') {
            & wsl.exe -d Ubuntu-22.04 -- kill -TERM ([int]$rawPid) 2>$null
        }
    }
    Remove-Item -LiteralPath $uncPath -Force -ErrorAction SilentlyContinue
}

if ($Mode -eq 'Production') {
    $compose = Join-Path $AppRoot 'deploy\docker\compose.yaml'
    switch ($Action) {
        'start' { docker compose -f $compose up -d --remove-orphans }
        'stop' { docker compose -f $compose down }
        'status' { docker compose -f $compose ps }
        'logs' { docker compose -f $compose logs --no-color --tail 200 --follow ls-vision mediamtx }
    }
    exit $LASTEXITCODE
}

$distro = 'Ubuntu-22.04'
$root = '/mnt/d/BusinessAnalyze/Camera'
$config = "$root/app/config/dev.yaml"
$runtime = '/opt/camera-safety-dev'
$hotReloadScript = "$root/app/deploy/dev/hot_reload.py"

function Invoke-DevWsl([string]$Command) {
    & wsl.exe -d $distro --user root -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed: $Command" }
}

function Follow-DevLogs {
    Write-Output 'Following WSL runtime logs; press Ctrl+C to stop following logs. Runtime remains active.'
    $command = @"
mkdir -p $runtime/logs
touch $runtime/logs/pipeline.log $runtime/logs/dashboard.log $runtime/logs/mediamtx.log $runtime/logs/hot-reload.log $runtime/logs/vite.stdout.log $runtime/logs/vite.stderr.log
exec tail -n 100 -F $runtime/logs/pipeline.log $runtime/logs/dashboard.log $runtime/logs/mediamtx.log $runtime/logs/hot-reload.log $runtime/logs/vite.stdout.log $runtime/logs/vite.stderr.log
"@
    & wsl.exe -d $distro --user root -- bash -lc ($command -replace "`r`n", "`n")
    $tailExitCode = $LASTEXITCODE
    if ($tailExitCode -notin @(0, 1, 2, 130)) {
        throw "WSL log follower failed with exit code $tailExitCode"
    }
}

switch ($Action) {
    'start' {
        $command = @"
set -euo pipefail
export PYTHONPATH=$root/app/src
export CAMERA_CONFIG=$config
if [ -f $root/.env.local ]; then export CAMERA_ENV_FILE=$root/.env.local; fi
# DeepStream prints one latency line per encoded frame when these flags exist.
# Remove them from the child environment; diagnostics stay opt-in.
unset NVDS_ENABLE_LATENCY_MEASUREMENT NVDS_ENABLE_COMPONENT_LATENCY_MEASUREMENT
export LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:`${LD_LIBRARY_PATH:-}
mkdir -p $runtime/logs $runtime/status $runtime/state $runtime/evidence
pkill -TERM -f '^python3 -m runner --config /mnt/d/BusinessAnalyze/Camera/app/config/dev.yaml$' || true
pkill -TERM -f '^python3 -m interfaces.dashboard_api$' || true
pkill -TERM -f '^/opt/camera-safety/mediamtx/mediamtx .*mediamtx.yml$' || true
pkill -TERM -f '^python3 /mnt/d/BusinessAnalyze/Camera/app/deploy/dev/hot_reload.py ' || true
pkill -TERM -f '^/usr/bin/python3 -m application.camera_worker' || true
pkill -TERM -f '^ffmpeg .*rtsp://127.0.0.1:8554/(face_mock|safety_mock)$' || true
sleep 1
setsid -f python3 $hotReloadScript --root $root --config $config --runtime $runtime --mediamtx-config $root/app/deploy/docker/mediamtx.yml --pid-file $runtime/status/hot-reload.pid >$runtime/logs/hot-reload.log 2>&1
sleep 0.5
echo 'native WSL development runtime started with backend hot reload'
"@
        Invoke-DevWsl ($command -replace "`r`n", "`n")
        Start-ViteDev
        Write-Output "Vite development server started on http://127.0.0.1:$VitePort/dashboard.html"
        if ($FollowLogs) { Follow-DevLogs }
    }
    'pause' {
        Stop-ViteDev
        Stop-WslPidFile $HotReloadPidPath
        Stop-WslPidFile '/opt/camera-safety-dev/status/runner.pid'
        Stop-WslPidFile '/opt/camera-safety-dev/status/dashboard.pid'
        Stop-WslPidFile '/opt/camera-safety-dev/status/mediamtx.pid'
        $command = @'
set -euo pipefail
pkill -TERM -f '^(/usr/bin/)?python3 -m runner --config /mnt/d/BusinessAnalyze/Camera/app/config/dev.yaml$' || true
pkill -TERM -f '^(/usr/bin/)?python3 /mnt/d/BusinessAnalyze/Camera/app/deploy/dev/hot_reload.py ' || true
pkill -TERM -f '^(/usr/bin/)?python3 -m application.camera_worker' || true
pkill -TERM -f '^(/usr/bin/)?python3 -m interfaces.dashboard_api$' || true
pkill -TERM -f '^/opt/camera-safety/mediamtx/mediamtx .*mediamtx.yml$' || true
pkill -TERM -f '^ffmpeg .*rtsp://127.0.0.1:8554/(face_mock|safety_mock)$' || true
for attempt in {1..60}; do
    if ! pgrep -f '^(/usr/bin/)?python3 -m runner --config /mnt/d/BusinessAnalyze/Camera/app/config/dev.yaml$' >/dev/null && \
       ! pgrep -f '^(/usr/bin/)?python3 /mnt/d/BusinessAnalyze/Camera/app/deploy/dev/hot_reload.py ' >/dev/null && \
       ! pgrep -f '^(/usr/bin/)?python3 -m application.camera_worker' >/dev/null && \
       ! pgrep -f '^(/usr/bin/)?python3 -m interfaces.dashboard_api$' >/dev/null && \
       ! pgrep -f '^/opt/camera-safety/mediamtx/mediamtx .*mediamtx.yml$' >/dev/null && \
       ! pgrep -x mediamtx >/dev/null && \
       ! pgrep -f '^ffmpeg .*rtsp://127.0.0.1:8554/(face_mock|safety_mock)$' >/dev/null; then
        exit 0
    fi
    sleep 0.5
done
echo 'native WSL development runtime did not stop within 30 seconds'
ps -eo pid,args | grep -E 'hot_reload|mediamtx|runner|application.camera_worker|interfaces.dashboard_api|ffmpeg.*mock' | grep -v grep || true
exit 1
'@
        Invoke-DevWsl ($command -replace "`r`n", "`n")
        Write-Output 'native WSL development runtime paused; WSL remains running'
    }
    'stop' {
        Stop-ViteDev
        Stop-WslPidFile $HotReloadPidPath
        Stop-WslPidFile '/opt/camera-safety-dev/status/runner.pid'
        Stop-WslPidFile '/opt/camera-safety-dev/status/dashboard.pid'
        Stop-WslPidFile '/opt/camera-safety-dev/status/mediamtx.pid'
        $command = @'
set -euo pipefail
        pkill -TERM -f '^(/usr/bin/)?python3 -m runner --config /mnt/d/BusinessAnalyze/Camera/app/config/dev.yaml$' || true
        pkill -TERM -f '^(/usr/bin/)?python3 /mnt/d/BusinessAnalyze/Camera/app/deploy/dev/hot_reload.py ' || true
        pkill -TERM -f '^(/usr/bin/)?python3 -m application.camera_worker' || true
        pkill -TERM -f '^(/usr/bin/)?python3 -m interfaces.dashboard_api$' || true
        pkill -TERM -f '^/opt/camera-safety/mediamtx/mediamtx .*mediamtx.yml$' || true
        pkill -TERM -f '^ffmpeg .*rtsp://127.0.0.1:8554/(face_mock|safety_mock)$' || true
        sleep 2
        pkill -KILL -f '^(/usr/bin/)?python3 -m runner --config /mnt/d/BusinessAnalyze/Camera/app/config/dev.yaml$' || true
        pkill -KILL -f '^(/usr/bin/)?python3 /mnt/d/BusinessAnalyze/Camera/app/deploy/dev/hot_reload.py ' || true
        pkill -KILL -f '^(/usr/bin/)?python3 -m application.camera_worker' || true
        pkill -KILL -f '^(/usr/bin/)?python3 -m interfaces.dashboard_api$' || true
        pkill -KILL -f '^/opt/camera-safety/mediamtx/mediamtx .*mediamtx.yml$' || true
pkill -KILL -f '^ffmpeg .*rtsp://127.0.0.1:8554/(face_mock|safety_mock)$' || true
'@
        Invoke-DevWsl ($command -replace "`r`n", "`n")
        & wsl.exe --shutdown
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to shut down WSL'
        }
        Write-Output 'native WSL development runtime stopped'
        Write-Output 'WSL shut down'
    }
    'status' {
        Invoke-DevWsl "ps -eo pid,args | grep -E 'hot_reload|mediamtx|runner|application.camera_worker|interfaces.dashboard_api|ffmpeg.*mock' | grep -v grep || true"
        $apiHttp = try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/health/live' -ErrorAction Stop).StatusCode } catch { 0 }
        $viteHttp = try { (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$VitePort/dashboard.html" -ErrorAction Stop).StatusCode } catch { 0 }
        Write-Output "dashboard_api_http=$apiHttp"
        Write-Output "vite_http=$viteHttp"
        $vitePid = if (Test-Path -LiteralPath $VitePidFile) { (Get-Content -LiteralPath $VitePidFile -Raw -Encoding utf8).Trim() } else { 'stopped' }
        Write-Output "vite_pid=$vitePid"
    }
    'logs' {
        Follow-DevLogs
    }
}
