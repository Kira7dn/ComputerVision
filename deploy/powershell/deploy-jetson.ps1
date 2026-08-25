<#
.SYNOPSIS
    Deploy the native Camera runtime to Jetson.

.DESCRIPTION
    This is the only supported owner of LS-Vision deployment. It publishes the
    apps source tree and never deploys or restarts the LeOS T-Box
    application through native systemd services.
#>

[CmdletBinding()]
param(
    [ValidateSet('deploy','status','rollback')]
    [string]$Action = 'deploy',
    [string]$JetsonAlias = 'jetson-nano',
    [string]$SudoPassword = $env:LS_VISION_SUDO_PASSWORD,
    [switch]$Development
)

$ErrorActionPreference = 'Stop'

$cameraRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$deployScript = Join-Path $PSScriptRoot 'deploy-jetson-dev.ps1'
if (-not (Test-Path -LiteralPath $deployScript -PathType Leaf)) {
    throw "Camera deploy implementation was not found: $deployScript"
}

if ($Action -ne 'deploy') {
    Write-Host "==> Camera Jetson $Action on $JetsonAlias" -ForegroundColor Cyan
} elseif ($Development) {
    Write-Host "==> Deploying Camera Jetson development runtime to $JetsonAlias" -ForegroundColor Cyan
} else {
    Write-Host "==> Deploying Camera native runtime to $JetsonAlias" -ForegroundColor Cyan
}

$deploymentProfile = if ($Development) { 'development' } else { 'production' }
$arguments = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $deployScript,
    '-Action', $Action,
    '-DeploymentProfile', $deploymentProfile,
    '-CameraRoot', $cameraRoot,
    '-RemoteHost', $JetsonAlias
)
if (-not [string]::IsNullOrWhiteSpace($SudoPassword)) {
    $arguments += @('-SudoPassword', $SudoPassword)
}
& powershell.exe @arguments
$deployExitCode = $LASTEXITCODE

if ($deployExitCode -ne 0) {
    throw "Jetson deployment failed with exit code $deployExitCode"
}

Write-Host "  OK  Camera runtime action completed: $Action" -ForegroundColor Green
