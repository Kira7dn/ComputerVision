param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)

# Compatibility entrypoint for native WSL development. Production operators use
# deploy/powershell/{start,stop,status}.ps1, which delegates to Compose.
& (Join-Path $PSScriptRoot 'deploy\powershell\start.ps1') -Action $Action -Mode Dev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
