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
$project = Join-Path $PSScriptRoot 'dahua_netsdk_backup\DahuaNetSdkBackup.csproj'
$output = Join-Path $PSScriptRoot '..\uploads\videos'
dotnet run --project $project -c Release -- `
    --host $HostAddress --port $Port --channels $Channels `
    --lookback-hours $LookbackHours --mode $Mode `
    --interval-seconds $IntervalSeconds --output-dir $output
