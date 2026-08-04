$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serverRoot = Join-Path $projectRoot 'server'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$config = Join-Path $PSScriptRoot 'mediamtx.yml'

# Using existing .venv, no setup script required
if (-not (Get-Command mediamtx -ErrorAction SilentlyContinue)) {
    throw 'Install MediaMTX: winget install --id bluenviron.mediamtx --exact'
}
if (Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue) {
    throw 'Port 8080 is already in use; stop the existing MediaServer first'
}

$env:ADAS_ENABLED = 'true'
$env:ADAS_CHANNELS = '2'
$env:ADAS_SUBTYPE = '0'
$env:ADAS_INPUT_FORMAT = 'mpegts'
$env:ADAS_DECODER_CODEC = 'hevc_cuvid'
$env:ADAS_MODEL_PATH = if ($env:ADAS_MODEL_PATH) { $env:ADAS_MODEL_PATH } else { Join-Path $projectRoot 'server\models\yolov8n.pt' }
$env:ULTRALYTICS_DISABLE_TENSORRT = '1'  # disable TensorRT backend


$mediaMtx = Start-Process -FilePath 'mediamtx' -ArgumentList @($config) `
    -WorkingDirectory $serverRoot -WindowStyle Normal -PassThru
try {
    Start-Sleep -Seconds 1
    $publicHost = if ($env:PUBLIC_HOST) { $env:PUBLIC_HOST } else { '127.0.0.1' }
    $env:PYTHONPATH = Join-Path $projectRoot 'server'
    & $python -m camera_server.media.service --public-host $publicHost
}
finally {
    if (-not $mediaMtx.HasExited) {
        Stop-Process -Id $mediaMtx.Id
    }
}
