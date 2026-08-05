param(
    [string]$HostAddress = '192.168.100.229',
    [int]$Port = 37777,
    [string]$Channels = '0,1,2,3,4,5,6,7',
    [int]$LookbackHours = 24,
    [ValidateSet('latest', 'all')][string]$Mode = 'latest',
    [int]$IntervalSeconds = 0
)

$ErrorActionPreference = 'Stop'
if (-not $env:DAHUA_PASSWORD) {
    throw 'Set DAHUA_PASSWORD before starting the backup worker.'
}
$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
$script = Join-Path $PSScriptRoot '..\server\camera_server\camera\hdd_downloader.py'
$output = Join-Path $env:CAMERA_RUNTIME_DIR 'uploads\videos'
$days = [Math]::Max(1, [Math]::Ceiling($LookbackHours / 24))
$env:PYTHONPATH = Join-Path $PSScriptRoot '..\server'
foreach ($channel in ($Channels -split ',')) {
    & $python $script --host $HostAddress --channel ([int]$channel) --lookback-days $days --output-dir $output
}
