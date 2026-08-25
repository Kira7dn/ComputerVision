[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$cameraRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$python = Join-Path $cameraRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Shared Python interpreter was not found: $python"
}

function Invoke-Checked([string]$Label, [scriptblock]$Command) {
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Push-Location $cameraRoot
try {
    Invoke-Checked 'pytest' { & $python -m pytest app\tests -q }
    Invoke-Checked 'ruff' { & $python -m ruff check app\src app\tests }
    Invoke-Checked 'compileall' { & $python -m compileall -q app\src app\tests }
    Invoke-Checked 'frontend build' { npm --prefix app\web run build }
    Invoke-Checked 'package.json parse' { Get-Content -LiteralPath 'package.json' -Raw -Encoding utf8 | ConvertFrom-Json | Out-Null }
    Invoke-Checked 'git diff check' { git diff --check }
    Write-Host 'OK all checks passed' -ForegroundColor Green
}
finally {
    Pop-Location
}
