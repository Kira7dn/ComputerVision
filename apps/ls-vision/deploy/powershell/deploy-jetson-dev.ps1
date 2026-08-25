<#
.SYNOPSIS
    Publish the isolated Jetson development runtime.

.DESCRIPTION
    Camera-owned development deploy implementation. package.json exposes the
    stable deploy entrypoint; this file is not a user-facing script.
#>

[CmdletBinding()]
param(
    [ValidateSet('deploy','start','status','rollback','cleanup')]
    [string]$Action = 'deploy',
    [ValidateSet('development','production')]
    [string]$DeploymentProfile = 'development',
    [string]$CameraRoot = '',
    [string]$RemoteHost = 'jetson-nano',
    [string]$SudoPassword = $env:LS_VISION_SUDO_PASSWORD,
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
printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' systemctl disable --now ls-vision-dev.service
'@
    $remoteStop = $remoteStop.Replace(([char]36 + 'cleanupPassword'), $cleanupPassword)
    $remoteStop = $remoteStop -replace "`r`n", "`n"
    $remoteStop | ssh $RemoteHost "tr -d '\r' | bash -s"
    exit $LASTEXITCODE
}

$cameraPath = if ($CameraRoot) {
    (Resolve-Path -LiteralPath $CameraRoot).Path
} else {
    (Resolve-Path (Join-Path $PSScriptRoot '..\..\..\..')).Path
}
$RemoteRoot = if ($RemoteRoot) { $RemoteRoot } elseif ($DeploymentProfile -eq 'production') { '/opt/ls-vision' } else { '/opt/ls-vision-dev' }
$appPath = Join-Path $cameraPath 'apps\ls-vision'
$modelsPath = Join-Path $cameraPath 'assets\models'
$frontModelPath = Join-Path $modelsPath 'openpilot\driving_supercombo.onnx'
$frontModelSha256 = '659727c4d4839adc4992a254409a54259a8756a743f2d567bf5fdc6579f8009b'
$serviceName = if ($DeploymentProfile -eq 'production') { 'ls-vision.service' } else { 'ls-vision-dev.service' }
$servicePath = Join-Path $appPath "deploy\systemd\$serviceName"
$mediaServicePath = Join-Path $appPath 'deploy\systemd\mediamtx.service'
$mdnsServicePath = Join-Path $appPath 'deploy\systemd\ls-vision-mdns.service'
$ingressServicePath = Join-Path $appPath 'deploy\systemd\ls-vision-ingress.service'

if ($Action -eq 'status') {
    ssh $RemoteHost "readlink -f '$RemoteRoot/current' 2>/dev/null || true; systemctl is-active '$serviceName' 2>/dev/null || true"
    exit $LASTEXITCODE
}

if ($Action -eq 'rollback') {
    if ($DeploymentProfile -ne 'production') { throw 'Rollback is production-only.' }
    if ([string]::IsNullOrWhiteSpace($SudoPassword)) { throw 'LS_VISION_SUDO_PASSWORD is required for rollback.' }
    $rollbackScript = @'
set -euo pipefail
REMOTE_ROOT='$RemoteRoot'
SUDO_PASSWORD='$SudoPassword'
sudo_cmd() { printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"; }
CURRENT=$(readlink -f "$REMOTE_ROOT/current" || true)
TARGET=''
for release in $(find "$REMOTE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-); do
  [ "$release" = "$CURRENT" ] && continue
  TARGET="$release"
  break
done
[ -n "$TARGET" ] || { echo 'No previous LS-Vision release is available.' >&2; exit 1; }
sudo_cmd ln -sfn "$TARGET" "$REMOTE_ROOT/current"
for unit in ls-vision.service mediamtx.service ls-vision-ingress.service; do
  source_unit="$TARGET/app/deploy/systemd/$unit"
  [ -f "$source_unit" ] && sudo_cmd install -m 644 "$source_unit" "/etc/systemd/system/$unit"
done
sudo_cmd systemctl daemon-reload
sudo_cmd systemctl restart mediamtx.service ls-vision.service ls-vision-ingress.service
for attempt in $(seq 1 30); do
  if curl --fail --silent --max-time 5 http://127.0.0.1:18080/health/ready >/dev/null; then
    echo "$TARGET"
    exit 0
  fi
  sleep 2
done
echo 'Rollback target did not become ready.' >&2
exit 1
'@
    $rollbackScript = $rollbackScript.Replace(([char]36 + 'RemoteRoot'), $RemoteRoot)
    $rollbackScript = $rollbackScript.Replace(([char]36 + 'SudoPassword'), $SudoPassword)
    $rollbackScript = $rollbackScript -replace "`r`n", "`n"
    $rollbackScript | ssh $RemoteHost "tr -d '\r' | bash -s"
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($SudoPassword)) {
    throw 'LS_VISION_SUDO_PASSWORD is required for deploy/start.'
}

foreach ($required in @($appPath, $servicePath, $mediaServicePath, $mdnsServicePath, $ingressServicePath, $frontModelPath, $Mock360Directory)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Jetson deployment input is missing: $required"
    }
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
        $processes += Start-Process -FilePath 'ssh' -ArgumentList @('-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-N','-L','18080:127.0.0.1:28080',$RemoteHost) -NoNewWindow -PassThru
        $processes += Start-Process -FilePath 'ssh' -ArgumentList @('-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-N','-L','8888:127.0.0.1:28888','-L','8889:127.0.0.1:28889',$RemoteHost) -NoNewWindow -PassThru
        $processes += Start-Process -FilePath $node -ArgumentList @($viteScript,'--host','0.0.0.0','--port',"$VitePort") -WorkingDirectory $webPath -NoNewWindow -PassThru
        $processes += Start-Process -FilePath $python -ArgumentList @('-m','ls_vision.interfaces.mock_media_server','--root',$Mock360Directory,'--host','127.0.0.1','--port',"$MockMediaPort") -WorkingDirectory $sourcePath -NoNewWindow -PassThru
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
printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' systemctl disable --now ls-vision-dev.service
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
$tempManifest = Join-Path ([System.IO.Path]::GetTempPath()) "ls-vision-release-$([guid]::NewGuid().ToString('N')).json"
$remoteArchive = "/tmp/$(Split-Path -Leaf $tempArchive)"
$remoteModelArchive = "/tmp/$(Split-Path -Leaf $tempModelArchive)"
$remoteMock360Archive = "/tmp/$(Split-Path -Leaf $tempMock360Archive)"
$remoteManifest = "/tmp/$(Split-Path -Leaf $tempManifest)"
$remoteService = "/tmp/$serviceName"
$remoteMediaService = '/tmp/mediamtx.service'
$remoteMdnsService = '/tmp/ls-vision-mdns.service'
$remoteIngressService = '/tmp/ls-vision-ingress.service'

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
    Write-Step "Packaging apps/ls-vision $DeploymentProfile source"
    $sourceCommit = (git -C $cameraPath rev-parse HEAD).Trim()
    $sourceDirty = [bool](git -C $cameraPath status --porcelain)
    $configPath = if ($DeploymentProfile -eq 'production') {
        Join-Path $appPath 'config\production.yaml'
    } else {
        Join-Path $appPath 'config\dev.yaml'
    }
    $manifestJson = [ordered]@{
        schema_version = 1
        release = $releaseName
        profile = $DeploymentProfile
        source_commit = $sourceCommit
        source_dirty = $sourceDirty
        config_sha256 = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
        front_model_sha256 = $frontModelSha256
        created_at = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempManifest, $manifestJson, $utf8NoBom)
    tar -czf $tempArchive --exclude='./web/node_modules' `
        --exclude='./.pytest_cache' --exclude='./.ruff_cache' --exclude='*/__pycache__' `
        --exclude='*.pyc' -C $appPath .
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package apps/ls-vision source' }
    if ((Get-FileHash -LiteralPath $frontModelPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $frontModelSha256) {
        throw 'Front model checksum does not match the pinned provenance record'
    }
    tar -czf $tempModelArchive -C $modelsPath openpilot/driving_supercombo.onnx
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package the front-assistance model' }
    tar -czf $tempMock360Archive -C $Mock360Directory CAM_FRONT.mp4 CAM_BACK.mp4 CAM_FRONT_LEFT.mp4 CAM_FRONT_RIGHT.mp4
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package synchronized 360 mock videos' }

    Write-Step "Copying Jetson $DeploymentProfile release $releaseName"
    scp -q $tempArchive "${RemoteHost}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy apps/ls-vision archive' }
    scp -q $tempModelArchive "${RemoteHost}:$remoteModelArchive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy front-assistance model archive' }
    scp -q $tempMock360Archive "${RemoteHost}:$remoteMock360Archive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy synchronized 360 mock videos' }
    scp -q $tempManifest "${RemoteHost}:$remoteManifest"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy release manifest' }
    scp -q $servicePath "${RemoteHost}:$remoteService"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy LS-Vision systemd unit' }
    if ($DeploymentProfile -eq 'production') {
        scp -q $mediaServicePath "${RemoteHost}:$remoteMediaService"
        if ($LASTEXITCODE -ne 0) { throw 'Unable to copy MediaMTX systemd unit' }
        scp -q $mdnsServicePath "${RemoteHost}:$remoteMdnsService"
        if ($LASTEXITCODE -ne 0) { throw 'Unable to copy LS-Vision mDNS systemd unit' }
        scp -q $ingressServicePath "${RemoteHost}:$remoteIngressService"
        if ($LASTEXITCODE -ne 0) { throw 'Unable to copy LS-Vision ingress systemd unit' }
    }

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
REMOTE_MDNS_SERVICE='$remoteMdnsService'
REMOTE_INGRESS_SERVICE='$remoteIngressService'

sudo_cmd() { printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"; }

if [ "$DEPLOYMENT_PROFILE" = production ]; then
  sudo_cmd systemctl stop ls-vision.service >/dev/null 2>&1 || true
  sudo_cmd systemctl stop mediamtx.service >/dev/null 2>&1 || true
else
  sudo_cmd systemctl stop ls-vision-dev.service >/dev/null 2>&1 || true
fi
sudo_cmd mkdir -p "$RELEASE_ROOT/app" "$REMOTE_ROOT/releases" "$REMOTE_ROOT/runtime" "$REMOTE_ROOT/data/status" "$REMOTE_ROOT/data/state" "$REMOTE_ROOT/data/evidence" "$REMOTE_ROOT/data/queue" "$REMOTE_ROOT/data/logs" "$REMOTE_ROOT/data/mock-videos/cameras/sync-lite" /opt/ls-vision/models/openpilot
sudo_cmd tar -xzf "$REMOTE_ARCHIVE" -C "$RELEASE_ROOT/app"
sudo_cmd install -m 644 '$remoteManifest' "$RELEASE_ROOT/release-manifest.json"
if [ "$DEPLOYMENT_PROFILE" = production ] || ! printf '%s  %s\n' "$FRONT_MODEL_SHA256" /opt/ls-vision/models/openpilot/driving_supercombo.onnx | sha256sum -c - >/dev/null 2>&1; then
  sudo_cmd tar -xzf "$REMOTE_MODEL_ARCHIVE" -C /opt/ls-vision/models
fi
sudo_cmd tar -xzf "$REMOTE_MOCK_360_ARCHIVE" -C "$REMOTE_ROOT/data/mock-videos/cameras/sync-lite"
sudo_cmd install -m 644 /dev/null "$REMOTE_ROOT/runtime/mediamtx.conf"
sudo_cmd rm -f -- /opt/ls-vision/models/openpilot/dmonitoring_model.onnx
printf '%s  %s\n' "$FRONT_MODEL_SHA256" /opt/ls-vision/models/openpilot/driving_supercombo.onnx | sha256sum -c -
sudo_cmd chown -R letron:letron "$RELEASE_ROOT"
sudo_cmd chown -R letron:letron "$REMOTE_ROOT/data"
sudo_cmd ln -sfn "$RELEASE_ROOT" "$REMOTE_ROOT/current"
sudo_cmd install -m 644 "$REMOTE_SERVICE" "/etc/systemd/system/$SERVICE_NAME"
if [ "$DEPLOYMENT_PROFILE" = production ]; then
  sudo_cmd install -m 644 "$REMOTE_MEDIA_SERVICE" /etc/systemd/system/mediamtx.service
  sudo_cmd install -m 644 "$REMOTE_MDNS_SERVICE" /etc/systemd/system/ls-vision-mdns.service
  sudo_cmd install -m 644 "$REMOTE_INGRESS_SERVICE" /etc/systemd/system/ls-vision-ingress.service
  sudo_cmd rm -f -- /etc/systemd/system/tbox.service.d/50-ls-vision-ingress.conf
fi
rm -f "$REMOTE_ARCHIVE" "$REMOTE_MODEL_ARCHIVE" "$REMOTE_MOCK_360_ARCHIVE" '$remoteManifest' "$REMOTE_SERVICE"
if [ "$DEPLOYMENT_PROFILE" = production ]; then rm -f "$REMOTE_MEDIA_SERVICE" "$REMOTE_MDNS_SERVICE" "$REMOTE_INGRESS_SERVICE"; fi
sudo_cmd systemctl daemon-reload
if [ "$DEPLOYMENT_PROFILE" = production ]; then
  while sudo_cmd iptables -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000 >/dev/null 2>&1; do :; done
  while sudo_cmd iptables -t nat -D OUTPUT -p tcp --dport 80 -j REDIRECT --to-port 8000 >/dev/null 2>&1; do :; done
  while sudo_cmd ip6tables -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000 >/dev/null 2>&1; do :; done
  while sudo_cmd ip6tables -t nat -D OUTPUT -p tcp --dport 80 -j REDIRECT --to-port 8000 >/dev/null 2>&1; do :; done
  sudo_cmd systemctl enable --now ls-vision-mdns.service
  sudo_cmd systemctl enable --now mediamtx.service
  sudo_cmd systemctl enable --now ls-vision.service
  sudo_cmd systemctl enable --now ls-vision-ingress.service
else
  sudo_cmd systemctl disable ls-vision-dev.service >/dev/null 2>&1 || true
  sudo_cmd systemctl start ls-vision-dev.service
fi

HEALTH_PORT=18080
if [ "$DEPLOYMENT_PROFILE" = development ]; then HEALTH_PORT=28080; fi
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error --max-time 5 --output /dev/null "http://127.0.0.1:$HEALTH_PORT/health/live" \
    && curl --fail --silent --show-error --max-time 5 --output /dev/null "http://127.0.0.1:$HEALTH_PORT/health/ready"; then
    if [ "$DEPLOYMENT_PROFILE" = production ]; then
      systemctl is-active --quiet ls-vision-ingress.service
      systemctl is-active --quiet ls-vision-mdns.service
      curl --fail --silent --show-error --max-time 5 --header 'Host: vision.local' --output /dev/null 'http://127.0.0.1/dashboard.html'
      curl --fail --silent --show-error --max-time 5 --header 'Host: vision.local' --output /dev/null 'http://127.0.0.1/health/ready'
      getent hosts vision.local >/dev/null
      if sudo_cmd iptables -t nat -S | grep -Eq -- '--dport 80 .*--to-ports? 8000|--dport 80 .*--to-port 8000'; then
        echo 'Global IPv4 NAT redirect from port 80 to 8000 is still present.' >&2
        exit 1
      fi
      if sudo_cmd ip6tables -t nat -S | grep -Eq -- '--dport 80 .*--to-ports? 8000|--dport 80 .*--to-port 8000'; then
        echo 'Global IPv6 NAT redirect from port 80 to 8000 is still present.' >&2
        exit 1
      fi
    fi
    retained=0
    for old_release in $(find "$REMOTE_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-); do
      [ "$old_release" = "$RELEASE_ROOT" ] && continue
      retained=$((retained + 1))
      [ "$retained" -le 2 ] && continue
      sudo_cmd rm -rf -- "$old_release"
    done
    sudo_cmd rm -f -- "$REMOTE_ROOT/data/status/hot-reload.pid"
    curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:$HEALTH_PORT/health/live"
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
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteManifest'), $remoteManifest)
    $remoteScript = $remoteScript.Replace(([char]36 + 'frontModelSha256'), $frontModelSha256)
    $remoteScript = $remoteScript.Replace(([char]36 + 'SudoPassword'), $SudoPassword)
    $remoteScript = $remoteScript.Replace(([char]36 + 'DeploymentProfile'), $DeploymentProfile)
    $remoteScript = $remoteScript.Replace(([char]36 + 'serviceName'), $serviceName)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteService'), $remoteService)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteMediaService'), $remoteMediaService)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteMdnsService'), $remoteMdnsService)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteIngressService'), $remoteIngressService)
    $remoteScript = $remoteScript -replace "`r`n", "`n"
    $remoteScript | ssh $RemoteHost "tr -d '\r' | bash -s"
    if ($LASTEXITCODE -ne 0) { throw "Jetson $DeploymentProfile deployment failed" }

    Write-OK "Jetson $DeploymentProfile runtime deployed and healthy"
}
finally {
    Remove-Item -LiteralPath $tempArchive, $tempModelArchive, $tempMock360Archive, $tempManifest -Force -ErrorAction SilentlyContinue
}
