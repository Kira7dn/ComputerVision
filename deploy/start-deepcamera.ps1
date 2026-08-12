$ErrorActionPreference = 'Stop'
param(
    [ValidateSet('nano','small','medium','large')]
    [string]$ModelSize = 'nano'
    [ValidateRange(0.1,1.0)]
    [double]$Confidence = 0.3
)

Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$detector = Join-Path $PSScriptRoot 'DeepCamera\skills\detection\yolo-detection-2026\scripts\detect.py'
$lib = Join-Path $PSScriptRoot 'DeepCamera\skills\lib'

if (-not (Test-Path -LiteralPath $python)) { throw "Python environment not found: $python" }
if (-not (Test-Path -LiteralPath $detector)) { throw "DeepCamera detector not found: $detector" }

$env:PYTHONPATH = $lib
& $python $detector --model-size $ModelSize --confidence $Confidence --device cuda
exit $LASTEXITCODE
