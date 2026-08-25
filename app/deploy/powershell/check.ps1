[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$cameraRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$python = Join-Path $cameraRoot '.venv\Scripts\python.exe'

& $python -m pytest -c (Join-Path $cameraRoot 'app\pyproject.toml') (Join-Path $cameraRoot 'app\tests') -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m ruff check (Join-Path $cameraRoot 'app\src') (Join-Path $cameraRoot 'app\tests')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m compileall -q (Join-Path $cameraRoot 'app\src') (Join-Path $cameraRoot 'app\tests')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

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
    (Join-Path $cameraRoot 'app') (Join-Path $cameraRoot 'docs') (Join-Path $cameraRoot 'AGENTS.md')
if ($LASTEXITCODE -eq 0) {
    $forbidden | Write-Host
    exit 1
}
if ($LASTEXITCODE -ne 1) { exit $LASTEXITCODE }

npm.cmd --prefix (Join-Path $cameraRoot 'app\web') run build
exit $LASTEXITCODE
