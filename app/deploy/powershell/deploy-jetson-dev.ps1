<##
.SYNOPSIS
    Publish the isolated Jetson development runtime.

.DESCRIPTION
    Internal adapter required by LeOS tbox_lab. package.json exposes only
    dev, check and deploy; this file is not a user-facing script.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CameraRoot,
    [string]$RemoteHost = 'jetson-nano',
    [Parameter(Mandatory = $true)]
    [string]$SudoPassword,
    [string]$RemoteRoot = '/opt/ls-vision-dev'
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-OK([string]$Message) { Write-Host "  OK  $Message" -ForegroundColor Green }

$cameraPath = (Resolve-Path -LiteralPath $CameraRoot).Path
$appPath = Join-Path $cameraPath 'app'
$servicePath = Join-Path $appPath 'deploy\systemd\ls-vision-dev.service'
foreach ($required in @($appPath, $servicePath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Jetson development deployment input is missing: $required"
    }
}

$releaseName = "release-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$releaseRoot = "$RemoteRoot/releases/$releaseName"
$tempArchive = Join-Path ([System.IO.Path]::GetTempPath()) "ls-vision-dev-$([guid]::NewGuid().ToString('N')).tar.gz"
$remoteArchive = "/tmp/$(Split-Path -Leaf $tempArchive)"

try {
    Write-Step "Checking Jetson $RemoteHost"
    if ((ssh -o ConnectTimeout=5 $RemoteHost 'echo OK' 2>$null) -ne 'OK') {
        throw "Cannot connect to Jetson alias $RemoteHost"
    }

    Write-Step 'Packaging Camera/app development source'
    tar -czf $tempArchive --exclude='app/web/node_modules' `
        --exclude='app/.pytest_cache' --exclude='app/.ruff_cache' --exclude='*/__pycache__' `
        --exclude='*.pyc' -C $cameraPath app
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package Camera/app development source' }

    Write-Step "Copying Jetson development release $releaseName"
    scp -q $tempArchive "${RemoteHost}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy Camera/app development archive' }
    scp -q $servicePath "${RemoteHost}:/tmp/ls-vision-dev.service"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to copy Jetson development systemd unit' }

    $remoteScript = @'
set -euo pipefail
REMOTE_ROOT='$RemoteRoot'
RELEASE_ROOT='$releaseRoot'
REMOTE_ARCHIVE='$remoteArchive'
SUDO_PASSWORD='$SudoPassword'

sudo_cmd() { printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"; }

sudo_cmd systemctl stop ls-vision.service >/dev/null 2>&1 || true
sudo_cmd systemctl stop ls-vision-dev.service >/dev/null 2>&1 || true
sudo_cmd mkdir -p "$RELEASE_ROOT" "$REMOTE_ROOT/releases" "$REMOTE_ROOT/data/status" "$REMOTE_ROOT/data/state" "$REMOTE_ROOT/data/evidence" "$REMOTE_ROOT/data/queue" "$REMOTE_ROOT/data/logs"
sudo_cmd tar -xzf "$REMOTE_ARCHIVE" -C "$RELEASE_ROOT"
sudo_cmd chown -R letron:letron "$RELEASE_ROOT"
sudo_cmd ln -sfn "$RELEASE_ROOT" "$REMOTE_ROOT/current"
sudo_cmd install -m 644 /tmp/ls-vision-dev.service /etc/systemd/system/ls-vision-dev.service
rm -f "$REMOTE_ARCHIVE" /tmp/ls-vision-dev.service
sudo_cmd systemctl daemon-reload
sudo_cmd systemctl enable --now ls-vision-dev.service

for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 5 --output /dev/null 'http://127.0.0.1:18080/health/live'; then
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
echo 'Jetson development health check timed out.' >&2
systemctl --no-pager --full status ls-vision-dev.service >&2 || true
exit 1
'@
    $remoteScript = $remoteScript.Replace(([char]36 + 'RemoteRoot'), $RemoteRoot)
    $remoteScript = $remoteScript.Replace(([char]36 + 'releaseRoot'), $releaseRoot)
    $remoteScript = $remoteScript.Replace(([char]36 + 'remoteArchive'), $remoteArchive)
    $remoteScript = $remoteScript.Replace(([char]36 + 'SudoPassword'), $SudoPassword)
    $remoteScript = $remoteScript -replace "`r`n", "`n"
    $remoteScript | ssh $RemoteHost "tr -d '\r' | bash -s"
    if ($LASTEXITCODE -ne 0) { throw 'Jetson development deployment failed' }

    Write-OK 'Jetson development runtime deployed and healthy'
}
finally {
    Remove-Item -LiteralPath $tempArchive -Force -ErrorAction SilentlyContinue
}
