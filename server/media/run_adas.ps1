$ErrorActionPreference = 'Stop'
$serverRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $serverRoot '.venv-adas\Scripts\python.exe'
$config = Join-Path $PSScriptRoot 'mediamtx.yml'

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run media/setup_adas.ps1 first'
}
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
$env:ADAS_MODEL_PATH = Join-Path $serverRoot 'models\yolov8n.engine'
$env:MEDIAMTX_PUBLISH_URL = 'rtsp://127.0.0.1:8554/adas-ch2'
$env:MEDIAMTX_WEBRTC_BASE = 'http://192.168.100.108:8889'

$mediaMtx = Start-Process -FilePath 'mediamtx' -ArgumentList @($config) `
    -WorkingDirectory $serverRoot -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 1
    & $python (Join-Path $PSScriptRoot 'server.py') --public-host 192.168.100.108
}
finally {
    if (-not $mediaMtx.HasExited) {
        Stop-Process -Id $mediaMtx.Id
    }
}
