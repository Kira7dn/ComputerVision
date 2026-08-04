$ErrorActionPreference = 'Stop'
$serverRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $serverRoot '.venv-adas'
$requirements = Join-Path $serverRoot 'requirements-adas.in'
$cachePath = Join-Path $serverRoot '.uv-cache'
$tempPath = Join-Path $serverRoot '.tmp'
New-Item -ItemType Directory -Force -Path $cachePath,$tempPath | Out-Null
$env:UV_CACHE_DIR = $cachePath
$env:TEMP = $tempPath
$env:TMP = $tempPath

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required; install uv before bootstrapping ADAS'
}

uv venv --python 3.12 $venvPath
$python = Join-Path $venvPath 'Scripts\python.exe'
uv pip install --python $python -r $requirements
uv pip install --python $python --index-url https://download.pytorch.org/whl/cu130 --reinstall `
    'torch==2.11.0' 'torchvision==0.26.0'

& $python -c "import tensorrt as trt; assert trt.Builder(trt.Logger()); print('TensorRT', trt.__version__)"
& $python -c "import torch; assert torch.cuda.is_available(); print('Torch CUDA', torch.__version__, torch.version.cuda)"
ffmpeg -hide_banner -hwaccels
ffmpeg -hide_banner -encoders | Select-String 'h264_nvenc'

Write-Host "ADAS environment ready: $venvPath"
Write-Host 'Install MediaMTX with: winget install --id bluenviron.mediamtx --exact'
