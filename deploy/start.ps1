$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $PSScriptRoot 'docker-compose.yml'
$sourceConfig = Join-Path $PSScriptRoot 'config.yml'
$runtimeConfig = 'E:\Docker\Frigate\config\config.yml'
$logDir = Join-Path $workspace '.tmp\runtime'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not (docker info 2>$null)) { throw 'Docker Engine is not available.' }

$old = Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" |
  Where-Object { $_.CommandLine -like '*18554*' -or $_.CommandLine -like '*mock_videos*' }
foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }

Copy-Item -LiteralPath $sourceConfig -Destination $runtimeConfig -Force
docker compose -f $compose up -d --no-build mediamtx frigate
Start-Sleep -Seconds 3

$car = Join-Path $workspace 'mock_videos\car-number-plate-video\cam-in\Traffic Control CCTV.mp4'
$face = Join-Path $workspace 'mock_videos\face-recognition\segments\01_P1E_S1_C1.mp4'
$normalize = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=15"
$common = "-re -stream_loop -1 -fflags +genpts -i"
$video = "-an -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -r 15 -g 30 -vsync cfr -muxdelay 0 -f rtsp -rtsp_transport tcp"
$gateIn = "$common `"$car`" -vf $normalize $video rtsp://127.0.0.1:18554/gate-in"
Start-Process ffmpeg.exe -ArgumentList $gateIn -WindowStyle Hidden -RedirectStandardOutput "$logDir\gate-in.out.log" -RedirectStandardError "$logDir\gate-in.err.log"
$faceStream = "$common `"$face`" -vf $normalize $video rtsp://127.0.0.1:18554/chokepoint-face"
Start-Process ffmpeg.exe -ArgumentList $faceStream -WindowStyle Hidden -RedirectStandardOutput "$logDir\face.out.log" -RedirectStandardError "$logDir\face.err.log"

Start-Sleep -Seconds 8
docker compose -f $compose ps
Get-Process ffmpeg -ErrorAction SilentlyContinue | Select-Object Id, ProcessName
