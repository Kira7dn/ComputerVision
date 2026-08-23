<#
.SYNOPSIS
    Deploy the native Camera runtime to Jetson through tbox_lab.

.DESCRIPTION
    This is the Camera workspace entrypoint for Jetson deployment. The
    deployment contract remains owned by LeOS tbox_lab: it deploys the T-Box
    application and then publishes the native LS-Vision runtime using the
    Camera/app source tree. It does not start the old Docker Compose runtime.
#>

[CmdletBinding()]
param(
    [string]$JetsonAlias = 'jetson-default',
    [string]$LeosRoot = 'D:\BusinessAnalyze\Letron\letron-leos'
)

$ErrorActionPreference = 'Stop'

$cameraRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$leosRootPath = (Resolve-Path -LiteralPath $LeosRoot).Path
$tboxLab = Join-Path $leosRootPath 'services\tbox\factory\tbox_lab.ps1'

if (-not (Test-Path -LiteralPath $tboxLab -PathType Leaf)) {
    throw "tbox_lab.ps1 was not found: $tboxLab"
}

Write-Host "==> Deploying Camera native runtime through tbox_lab to $JetsonAlias" -ForegroundColor Cyan
Push-Location $leosRootPath
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tboxLab `
        deploy-app `
        -JetsonAlias $JetsonAlias `
        -CameraRoot $cameraRoot
    $deployExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($deployExitCode -ne 0) {
    throw "Jetson deployment failed with exit code $deployExitCode"
}

Write-Host '  OK  Camera native runtime deployed through tbox_lab' -ForegroundColor Green
