<#
.SYNOPSIS
    Publish the isolated Jetson development runtime.

.DESCRIPTION
    Camera-owned development deploy implementation. package.json exposes the
    stable deploy entrypoint; this file is not a user-facing script.
#>

[CmdletBinding()]
param(
    [ValidateSet('deploy','start','status','cleanup')]
    [string]$Action = 'deploy',
    [ValidateSet('development','production')]
    [string]$DeploymentProfile = 'development',
    [string]$CameraRoot = '',
    [string]$RemoteHost = 'jetson-nano',
    [string]$SudoPassword = 'letron123',
    [string]$RemoteRoot = '',
    [string]$Mock360Directory = 'E:\v1.0-mini\sweeps\videos\sync-lite',
    [int]$VitePort = 5173,
    [int]$MockMediaPort = 18081,
    [int]$ParentProcessId = 0
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-OK([string]$Message) { Write-Host "  OK  $Message" -ForegroundColor Green }

if ($Action -eq 'cleanup') {
    if ($ParentProcessId -le 0) { throw 'cleanup requires ParentProcessId' }
    while (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue) {
        Start-Sleep -Milliseconds 500
    }
    $cleanupPassword = $env:LS_VISION_DEV_WATCHDOG_SUDO_PASSWORD
    if ([string]::IsNullOrWhiteSpace($cleanupPassword)) { $cleanupPassword = $SudoPassword }
    $remoteStop = @'
set -e
SUDO_PASSWORD='$cleanupPassword'
printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' systemctl stop ls-vision-dev.service
'@
    $remoteStop = $remoteStop.Replace(([char]36 + 'cleanupPassword'), $cleanupPassword)
    $remoteStop = $remoteStop -replace "`r`n", "`n"
    $remoteStop | ssh $RemoteHost "tr -d '\r' | bash -s"
    exit $LASTEXITCODE
}

$cameraPath = if ($CameraRoot) {
    (Resolve-Path -LiteralPath $CameraRoot).Path
} else {
    (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}
$RemoteRoot = if ($RemoteRoot) { $RemoteRoot } elseif ($DeploymentProfile -eq 'production') { '/opt/ls-vision' } else { '/opt/ls-vision-dev' }
$appPath = Join-Path $cameraPath 'app'
$modelsPath = Join-Path $cameraPath 'assets\models'
$frontModelPath = Join-Path $modelsPath 'openpilot\driving_supercombo.onnx'
$frontModelSha256 = '659727c4d4839adc4992a254409a54259a8756a743f2d567bf5fdc6579f8009b'
$serviceName = if ($DeploymentProfile -eq 'production') { 'ls-vision.service' } else { 'ls-vision-dev.service' }
$servicePath = Join-Path $appPath "deploy\systemd\$serviceName"
$mediaServicePath = Join-Path $appPath 'deploy\systemd\mediamtx.service'
foreach ($required in @($appPath, $servicePath, $mediaServicePath, $frontModelPath, $Mock360Directory)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Jetson development deployment input is missing: $required"
    }
}

if ($Action -eq 'status') {
    ssh $RemoteHost "systemctl is-active $serviceName 2>/dev/null || true"
    exit $LASTEXITCODE
}

if ($Action -eq 'start') {
    if ($DeploymentProfile -ne 'development') { throw 'Interactive start is development-only.' }
    & $PSCommandPath -Action deploy -DeploymentProfile development -CameraRoot $cameraPath -RemoteHost $RemoteHost -SudoPassword $SudoPassword -Mock360Directory $Mock360Directory
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $env:LS_VISION_DEV_WATCHDOG_SUDO_PASSWORD = $SudoPassword
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
            '-Action', 'cleanup', '-RemoteHost', $RemoteHost, '-ParentProcessId', "$PID"
        ) -WindowStyle Hidden > $null
    }
    finally {
        Remove-Item Env:LS_VISION_DEV_WATCHDOG_SUDO_PASSWORD -ErrorAction SilentlyContinue
    }

    $processes = @()
    try {
        $webPath = Join-Path $appPath 'web'
        $sourcePath = Join-Path $appPath 'src'
        $syncScript = Join-Path $appPath 'deploy\dev\jetson_sync.py'
        $python = Join-Path $cameraPath '.venv\Scripts\python.exe'
        $node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
        if (-not $node) { throw 'node.exe is required for Vite.' }
        $viteScript = Join-Path $webPath 'node_modules\vite\bin\vite.js'
        foreach ($required in @($python, $syncScript, $viteScript)) {
            if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Development input is missing: $required" }
        }
        foreach ($port in @($VitePort, $MockMediaPort, 18080, 8888, 8889)) {
            if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
                throw "Required development port is already in use: $port"
            }
        }
        $processes += Start-Process -FilePath $python -ArgumentList @($syncScript, '--root', $cameraPath, '--jetson', $RemoteHost) -WorkingDirectory $cameraPath -NoNewWindow -PassThru
        $processes += Start-Process -FilePath 'ssh' -ArgumentList @('-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-N','-L','18080:127.0.0.1:18080',$RemoteHost) -NoNewWindow -PassThru
        $processes += Start-Process -FilePath 'ssh' -ArgumentList @('-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-N','-L','8888:127.0.0.1:8888','-L','8889:127.0.0.1:8889',$RemoteHost) -NoNewWindow -PassThru
        $processes += Start-Process -FilePath $node -ArgumentList @($viteScript,'--host','0.0.0.0','--port',"$VitePort") -WorkingDirectory $webPath -NoNewWindow -PassThru
        $processes += Start-Process -FilePath $python -ArgumentList @('-m','interfaces.mock_media_server','--root',$Mock360Directory,'--host','127.0.0.1','--port',"$MockMediaPort") -WorkingDirectory $sourcePath -NoNewWindow -PassThru
        Write-OK "Development endpoint active at http://127.0.0.1:$VitePort/dashboard.html"
        while ($true) {
            Start-Sleep -Seconds 1
            foreach ($process in $processes) { if ($process.HasExited) { throw "Development process exited: $($process.Id)" } }
        }
    }
    finally {
        foreach ($process in @($processes | Select-Object -Reverse)) {
            if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        }
        $remoteStop = @'
set -e
SUDO_PASSWORD='$SudoPassword'
printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' systemctl stop ls-vision-dev.service
'@
        $remoteStop = $remoteStop.Replace(([char]36 + 'SudoPassword'), $SudoPassword)
        $remoteStop = $remoteStop -replace "`r`n", "`n"
        $remoteStop | ssh $RemoteHost "tr -d '\r' | bash -s"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Unable to stop remote ls-vision-dev.service during development cleanup.'
        }
    }
    exit 0
}

$releaseName = "release-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$releaseRoot = "$RemoteRoot/releases/$releaseName"
$tempArchive = Join-Path ([System.IO.Path]::GetTempPath()) "ls-vision-dev-$([guid]::NewGuid().ToString('N')).tar.gz"
$tempModelArchive = Join-Path ([System.IO.Path]::GetTempPath()) "ls-vision-dev-models-$([guid]::NewGuid().ToString('N')).tar.gz"
$tempMock360Archive = Join-Path ([System.IO.Path]::GetTempPath()) "ls-vision-dev-mock-360-$([guid]::NewGuid().ToString('N')).tar.gz"
$remoteArchive = "/tmp/$(Split-Path -Leaf $tempArchive)"
$remoteModelArchive = "/tmp/$(Split-Path -Leaf $tempModelArchive)"
$remoteMock360Archive = "/tmp/$(Split-Path -Leaf $tempMock360Archive)"
$remoteService = "/tmp/$serviceName"
$remoteMediaService = '/tmp/mediamtx.service'

try {
    Write-Step "Checking Jetson $RemoteHost"
    if ((ssh -o ConnectTimeout=5 $RemoteHost 'echo OK' 2>$null) -ne 'OK') {
        throw "Cannot connect to Jetson alias $RemoteHost"
    }

    if ($DeploymentProfile -eq 'production') {
        Write-Step 'Building production dashboard'
        npm.cmd --prefix (Join-Path $appPath 'web') run build
        if ($LASTEXITCODE -ne 0) { throw 'Unable to build production dashboard' }
    }
    Write-Step "Packaging Camera/app $DeploymentProfile source"
    tar -czf $tempArchive --exclude='app/web/node_modules' `
        --exclude='app/.pytest_cache' --exclude='app/.ruff_cache' --exclude='*/__pycache__' `
        --exclude='*.pyc' -C $cameraPath app
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package Camera/app development source' }
    if ((Get-FileHash -LiteralPath $frontModelPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $frontModelSha256) {
        throw 'Front model checksum does not match the pinned provenance record'
    }
    tar -czf $tempModelArchive -C $modelsPath openpilot/driving_supercombo.onnx
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package the front-assistance model' }
    tar -czf $tempMock360Archive -C $Mock360Directory CAM_FRONT.mp4 CAM_BACK.mp4 CAM_FRONT_LEFT.mp4 CAM_FRONT_RIGHT.mp4
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package synchronized 360 mock videos' }

    Write-Step "Copying Jetson $DeploymentProfile release $releaseName"
    scp -q $tempArchive "${RemoteHost}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy Camera/app development archive' }
    scp -q $tempModelArchive "${RemoteHost}:$remoteModelArchive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy front-assistance model archive' }
    scp -q $tempMock360Archive "${RemoteHost}:$remoteMock360Archive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy synchronized 360 mock videos' }
    scp -q $servicePath "${RemoteHost}:$remoteService"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy LS-Vision systemd unit' }
    scp -q $mediaServicePath "${RemoteHost}:$remoteMediaService"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy MediaMTX systemd unit' }

    $remoteScript = @'
set -euo pipefail
REMOTE_ROOT='$RemoteRoot'
RELEASE_ROOT='$releaseRoot'
REMOTE_ARCHIVE='$remoteArchive'
REMOTE_MODEL_ARCHIVE='$remoteModelArchive'
FRONT_MODEL_SHA256='$frontModelSha256'
SUDO_PASSWORD='$SudoPassword'
DEPLOYMENT_PROFILE='$DeploymentProfile'
SERVICE_NAME='$serviceName'
REMOTE_SERVICE='$remoteService'
REMOTE_MEDIA_SERVICE='$remoteMediaService'

sudo_cmd() { printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"; }

sudo_cmd systemctl stop ls-vision.service >/dev/null 2>&1 || true
sudo_cmd systemctl stop ls-vision-dev.service >/dev/null 2>&1 || true
sudo_cmd systemctl stop mediamtx.service >/dev/null 2>&1 || true
sudo_cmd mkdir -p "$RELEASE_ROOT" "$REMOTE_ROOT/releases" "$REMOTE_ROOT/data/status" "$REMOTE_ROOT/data/state" "$REMOTE_ROOT/data/evidence" "$REMOTE_ROOT/data/queue" "$REMOTE_ROOT/data/logs" "$REMOTE_ROOT/data/mock-videos/cameras/sync-lite" /opt/ls-vision/models/openpilot
sudo_cmd tar -xzf "$REMOTE_ARCHIVE" -C "$RELEASE_ROOT"
sudo_cmd tar -xzf "$REMOTE_MODEL_ARCHIVE" -C /opt/ls-vision/models
sudo_cmd tar -xzf "$REMOTE_MOCK_360_ARCHIVE" -C "$REMOTE_ROOT/data/mock-videos/cameras/sync-lite"
sudo_cmd install -m 644 /dev/null /opt/ls-vision/runtime/mediamtx.conf
sudo_cmd rm -f -- /opt/ls-vision/models/openpilot/dmonitoring_model.onnx
printf '%s  %s\n' "$FRONT_MODEL_SHA256" /opt/ls-vision/models/openpilot/driving_supercombo.onnx | sha256sum -c -
sudo_cmd chown -R letron:letron "$RELEASE_ROOT"
sudo_cmd ln -sfn "$RELEASE_ROOT" "$REMOTE_ROOT/current"
sudo_cmd install -m 644 "$REMOTE_SERVICE" "/etc/systemd/system/$SERVICE_NAME"
sudo_cmd install -m 644 "$REMOTE_MEDIA_SERVICE" /etc/systemd/system/mediamtx.service
rm -f "$REMOTE_ARCHIVE" "$REMOTE_MODEL_ARCHIVE" "$REMOTE_MOCK_360_ARCHIVE" "$REMOTE_SERVICE" "$REMOTE_MEDIA_SERVICE"
sudo_cmd systemctl daemon-reload
if [ "$DEPLOYMENT_PROFILE" = production ]; then
  sudo_cmd systemctl enable --now mediamtx.service
  sudo_cmd systemctl enable --now ls-vision.service
else
  sudo_cmd systemctl enable --now ls-vision-dev.service
fi

for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error --max-time 5 --output /dev/null 'http://127.0.0.1:18080/health/live' \
    && curl --fail --silent --show-error --max-time 5 --output /dev/null 'http://127.0.0.1:18080/health/ready'; then
    for old_release in "$REMOTE_ROOT"/releases/*; do
      [ "$old_release" = "$RELEASE_ROOT" ] && continue
      [ -d "$old_release" ] || continue
      sudo_cmd rm -rf -- "$old_release"
    done
    sudo_cmd rm -f -- "$REMOTE_ROOT/data/status/hot-reload.pid"
    curl --fail --silent --show-error --max-time 5 'http://127.0.0.1:18080/health/live'
    exit 0
  fi
  sleep 2
done
echo "Jetson $DEPLOYMENT_PROFILE readiness check timed out." >&2
systemctl --no-pager --full status "$SERVICE_NAME" >&2 || true
exit 1
'@
    $remoteScript = $remoteScript.Replace(([char]36 + 'RemoteRoot'), $RemoteRoot)
    $remoteScript = $remoteScript.Replace(([char]36 + 'releaseRoot'), $releaseRoot)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteArchive'), $remoteArchive)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteModelArchive'), $remoteModelArchive)
    $remoteScript = $remoteScript.Replace(([char]36 + 'REMOTE_MOCK_360_ARCHIVE'), $remoteMock360Archive)
    $remoteScript = $remoteScript.Replace(([char]36 + 'frontModelSha256'), $frontModelSha256)
    $remoteScript = $remoteScript.Replace(([char]36 + 'SudoPassword'), $SudoPassword)
    $remoteScript = $remoteScript.Replace(([char]36 + 'DeploymentProfile'), $DeploymentProfile)
    $remoteScript = $remoteScript.Replace(([char]36 + 'serviceName'), $serviceName)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteService'), $remoteService)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteMediaService'), $remoteMediaService)
    $remoteScript = $remoteScript -replace "`r`n", "`n"
    $remoteScript | ssh $RemoteHost "tr -d '\r' | bash -s"
    if ($LASTEXITCODE -ne 0) { throw "Jetson $DeploymentProfile deployment failed" }

    Write-OK "Jetson $DeploymentProfile runtime deployed and healthy"
}
finally {
    Remove-Item -LiteralPath $tempArchive, $tempModelArchive, $tempMock360Archive -Force -ErrorAction SilentlyContinue
}
