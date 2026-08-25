[CmdletBinding()]
param(
    [ValidateSet('start','status')]
    [string]$Action = 'start',
    [string]$JetsonAlias = 'jetson-nano',
    [string]$CameraRoot = '',
    [int]$VitePort = 5173,
    [int]$MockMediaPort = 18081,
    [string]$MockMediaDirectory = 'E:\v1.0-mini\sweeps\videos\sync-lite'
)

$ErrorActionPreference = 'Stop'
$cameraPath = if ($CameraRoot) {
    (Resolve-Path -LiteralPath $CameraRoot).Path
} else {
    (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}
$webPath = Join-Path $cameraPath 'app\web'
$sourcePath = Join-Path $cameraPath 'app\src'
$syncScript = Join-Path $cameraPath 'app\deploy\dev\jetson_sync.py'
$python = Join-Path $cameraPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = 'python' }

if ($Action -eq 'status') {
    $api = try { (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18080/health/live' -ErrorAction Stop).StatusCode } catch { 0 }
    $vite = try { (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$VitePort/dashboard.html" -ErrorAction Stop).StatusCode } catch { 0 }
    $mockMedia = try { (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$MockMediaPort/" -ErrorAction Stop).StatusCode } catch { 0 }
    Write-Output "jetson_api_tunnel_http=$api"
    Write-Output "vite_http=$vite"
    Write-Output "mock_media_http=$mockMedia"
    exit 0
}

if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
    throw "Jetson sync script was not found: $syncScript"
}
if (-not (Test-Path -LiteralPath $webPath -PathType Container)) {
    throw "Camera web directory was not found: $webPath"
}
if (-not (Test-Path -LiteralPath $MockMediaDirectory -PathType Container)) {
    throw "Synchronized mock media directory was not found: $MockMediaDirectory"
}
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if (-not $node) {
    throw 'node.exe is required for local Vite HMR.'
}
$viteScript = Join-Path $webPath 'node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $viteScript -PathType Leaf)) {
    throw 'Vite dependencies are not installed; run npm install in app\web first.'
}
foreach ($port in @($VitePort, $MockMediaPort, 18080, 8888, 8889)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Required Jetson dev port $port is already owned by an untracked process."
    }
}

$script:syncProcess = $null
$script:apiTunnelProcess = $null
$script:streamTunnelProcess = $null
$script:viteProcess = $null
$script:mockMediaProcess = $null
try {
    $script:syncProcess = Start-Process -FilePath $python -ArgumentList @(
        $syncScript, '--root', $cameraPath, '--jetson', $JetsonAlias
    ) -WorkingDirectory $cameraPath -NoNewWindow -PassThru

    $script:apiTunnelProcess = Start-Process -FilePath 'ssh' -ArgumentList @(
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=3',
        '-o', 'ServerAliveCountMax=2', '-N',
        '-L', '18080:127.0.0.1:18080',
        $JetsonAlias
    ) -NoNewWindow -PassThru

    $script:streamTunnelProcess = Start-Process -FilePath 'ssh' -ArgumentList @(
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=3',
        '-o', 'ServerAliveCountMax=2', '-N',
        '-L', '8888:127.0.0.1:8888',
        '-L', '8889:127.0.0.1:8889',
        $JetsonAlias
    ) -NoNewWindow -PassThru

    Start-Sleep -Milliseconds 700
    if ($script:apiTunnelProcess.HasExited -or $script:streamTunnelProcess.HasExited) {
        throw 'SSH API tunnel exited. Stop another local service using port 18080 and retry.'
    }

    $script:viteProcess = Start-Process -FilePath $node -ArgumentList @(
        $viteScript, '--host', '0.0.0.0', '--port', "$VitePort"
    ) -WorkingDirectory $webPath -NoNewWindow -PassThru

    $script:mockMediaProcess = Start-Process -FilePath $python -ArgumentList @(
        '-m', 'interfaces.mock_media_server', '--root', $MockMediaDirectory,
        '--host', '127.0.0.1', '--port', "$MockMediaPort"
    ) -WorkingDirectory $sourcePath -WindowStyle Hidden -PassThru

    Write-Host "Jetson dev runtime active: http://127.0.0.1:$VitePort/dashboard.html" -ForegroundColor Green
    Write-Host 'Backend changes sync to Jetson and restart automatically; frontend changes use Vite HMR.' -ForegroundColor Cyan
    Write-Host "Synchronized mock media is served locally from $MockMediaDirectory." -ForegroundColor Cyan
    Write-Host 'Press Ctrl+C to stop the local sync, tunnel and Vite processes.' -ForegroundColor Yellow

    while ($true) {
        Start-Sleep -Seconds 1
        foreach ($process in @($script:syncProcess, $script:apiTunnelProcess, $script:streamTunnelProcess, $script:viteProcess, $script:mockMediaProcess)) {
            if ($process.HasExited) {
                throw "Jetson dev process exited: $($process.Id)"
            }
        }
    }
}
finally {
    foreach ($process in @($script:mockMediaProcess, $script:viteProcess, $script:streamTunnelProcess, $script:apiTunnelProcess, $script:syncProcess)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
