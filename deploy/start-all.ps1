# Single composition root. Set DAHUA_PASSWORD before running.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$envFile = Join-Path $PSScriptRoot '.env.local'
if (Test-Path -LiteralPath $envFile) {
    Get-Content -Encoding utf8 $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+?)\s*=\s*(.+?)\s*$') {
            [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
        }
    }
}
if (-not $env:DAHUA_PASSWORD) { throw 'DAHUA_PASSWORD must be set in .env.local or environment' }
if (-not $env:DAHUA_FTP_PASSWORD) { $env:DAHUA_FTP_PASSWORD = $env:DAHUA_PASSWORD }
$env:ADAS_ENABLED = if ($env:ADAS_ENABLED) { $env:ADAS_ENABLED } else { 'true' }
$env:ADAS_CHANNELS = if ($env:ADAS_CHANNELS) { $env:ADAS_CHANNELS } else { '2' }
$env:ADAS_SUBTYPE = if ($env:ADAS_SUBTYPE) { $env:ADAS_SUBTYPE } else { '0' }
$env:ADAS_INPUT_FORMAT = if ($env:ADAS_INPUT_FORMAT) { $env:ADAS_INPUT_FORMAT } else { 'mpegts' }
$env:ADAS_DECODER_CODEC = if ($env:ADAS_DECODER_CODEC) { $env:ADAS_DECODER_CODEC } else { 'hevc_cuvid' }
$env:ULTRALYTICS_DISABLE_TENSORRT = '0'
$env:PUBLIC_HOST = if ($env:PUBLIC_HOST) { $env:PUBLIC_HOST } else { '127.0.0.1' }
$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python).Source }
$env:PYTHONPATH = Join-Path $PSScriptRoot 'server'
& $python -m camera_server.main
exit $LASTEXITCODE
