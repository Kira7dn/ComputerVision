param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$Distro = 'Ubuntu-22.04'
$Root = '/mnt/d/BusinessAnalyze/Camera'
$Runtime = '/opt/camera-deepstream'

function Invoke-WSL([string]$Command) {
    & wsl.exe -d $Distro --user root -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed: $Command" }
}

switch ($Action) {
    'start' {
        Invoke-WSL "mkdir -p $Runtime/logs; if [ ! -x $Runtime/mediamtx/mediamtx ]; then echo 'MediaMTX is not installed. Run the setup command from docs/DeepStream.md.' >&2; exit 2; fi; nohup $Runtime/mediamtx/mediamtx $Root/deepstream_safety/mediamtx.yml >$Runtime/logs/mediamtx.log 2>&1 & nohup python3 $Root/deepstream_safety/dashboard_server.py >$Runtime/logs/dashboard.log 2>&1 & sleep 2; setsid -f bash -c 'exec python3 $Root/deepstream_safety/pipeline.py --config $Root/deepstream_safety/config.yaml >$Runtime/logs/pipeline.log 2>&1 </dev/null'; echo started"
        Write-Output 'Dashboard: http://localhost:8080/dashboard.html'
    }
    'stop' {
        Invoke-WSL "pkill -f 'python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/[p]ipeline.py' 2>/dev/null || true; pkill -f '[d]ashboard_server.py' 2>/dev/null || true; pkill -f '[p]ython3 -m http.server 8080' 2>/dev/null || true; pkill -x ffmpeg 2>/dev/null || true; pkill -x mediamtx 2>/dev/null || true; echo stopped"
    }
    'status' {
        Invoke-WSL "pgrep -af '[m]ediamtx|[d]ashboard_server.py|[p]ython3 -m http.server 8080|[f]fmpeg.*safety_mock|[p]ipeline.py' || true; tail -n 20 $Runtime/logs/pipeline.log 2>/dev/null || true"
    }
}
