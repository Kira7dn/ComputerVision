$ErrorActionPreference = 'Stop'
$compose = Join-Path $PSScriptRoot 'docker-compose.yml'
$old = Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" |
  Where-Object { $_.CommandLine -like '*18554*' -or $_.CommandLine -like '*mock_videos*' }
foreach ($p in $old) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
docker compose -f $compose down
