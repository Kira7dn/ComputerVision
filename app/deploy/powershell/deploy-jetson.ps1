<#
.SYNOPSIS
    Deploy the native Camera runtime to Jetson.

.DESCRIPTION
    This is the only supported owner of LS-Vision deployment. It publishes the
    Camera/app source tree and never deploys or restarts the LeOS T-Box
    application. It does not start the old Docker Compose runtime.
#>

[CmdletBinding()]
param(
    [string]$JetsonAlias = 'jetson-nano',
    [string]$SudoPassword = 'letron123',
    [switch]$Development
)

$ErrorActionPreference = 'Stop'

$cameraRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$deployScript = Join-Path $PSScriptRoot 'deploy-jetson-dev.ps1'
if (-not (Test-Path -LiteralPath $deployScript -PathType Leaf)) {
    throw "Camera deploy implementation was not found: $deployScript"
}

if ($Development) {
    Write-Host "==> Deploying Camera Jetson development runtime to $JetsonAlias" -ForegroundColor Cyan
} else {
    Write-Host "==> Deploying Camera native runtime to $JetsonAlias" -ForegroundColor Cyan
}

$deploymentProfile = if ($Development) { 'development' } else { 'production' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $deployScript `
    -Action deploy `
    -DeploymentProfile $deploymentProfile `
    -CameraRoot $cameraRoot `
    -RemoteHost $JetsonAlias `
    -SudoPassword $SudoPassword
$deployExitCode = $LASTEXITCODE

if ($deployExitCode -ne 0) {
    throw "Jetson deployment failed with exit code $deployExitCode"
}

Write-Host '  OK  Camera runtime deployed by the Camera workspace' -ForegroundColor Green
