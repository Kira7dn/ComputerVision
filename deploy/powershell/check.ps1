[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$cameraRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$appRoot = Join-Path $cameraRoot 'apps'
$trainingRoot = Join-Path $cameraRoot 'training'
$trainingSources = @(
    (Join-Path $trainingRoot 'prepare_fire_smoke_dataset.py'),
    (Join-Path $trainingRoot 'train_fire_smoke.py'),
    (Join-Path $trainingRoot 'models')
)
$testsRoot = Join-Path $cameraRoot 'tests'
$python = Join-Path $cameraRoot '.venv\Scripts\python.exe'

& $python -m pytest -c (Join-Path $cameraRoot 'pyproject.toml') $testsRoot -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m ruff check (Join-Path $appRoot 'src') @trainingSources $testsRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m compileall -q (Join-Path $appRoot 'src') @trainingSources $testsRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$cameraServerRoot = Join-Path $cameraRoot 'services\camera-server'
Push-Location $cameraServerRoot
try {
    & $python -m ruff check camera_server tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & $python -m compileall -q camera_server tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

$legacyPattern = @(
    ('fri' + 'gate'),
    ('docker' + ' compose'),
    ('/opt/' + 'camera-safety'),
    ('/mnt/d/' + 'BusinessAnalyze/Camera')
) -join '|'
$forbidden = & rg -n -i `
    --glob '!web/package-lock.json' `
    --glob '!**/check.ps1' `
    --glob '*.py' --glob '*.toml' --glob '*.yaml' --glob '*.yml' --glob '*.md' --glob '*.ps1' --glob '*.service' `
    $legacyPattern `
    $appRoot $trainingRoot $testsRoot (Join-Path $cameraRoot 'config') (Join-Path $cameraRoot 'deploy') `
    (Join-Path $cameraRoot 'docs') (Join-Path $cameraRoot 'AGENTS.md')
if ($LASTEXITCODE -eq 0) {
    $forbidden | Write-Host
    exit 1
}
if ($LASTEXITCODE -ne 1) { exit $LASTEXITCODE }

npm.cmd --prefix (Join-Path $appRoot 'web') run build
exit $LASTEXITCODE
