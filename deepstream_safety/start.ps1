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
        Write-Output 'Dashboard: http://127.0.0.1:18080/dashboard.html'
        Start-Process 'http://127.0.0.1:18080/dashboard.html'
        $startCommand = @'
set -e -o pipefail
mkdir -p __RUNTIME__/logs
mkdir -p __RUNTIME__/status
if [ ! -x __RUNTIME__/mediamtx/mediamtx ]; then
  echo 'MediaMTX is not installed. Run the setup command from docs/DeepStream.md.' >&2
  exit 2
fi

__RUNTIME__/mediamtx/mediamtx __ROOT__/deepstream_safety/mediamtx.yml >__RUNTIME__/logs/mediamtx.log 2>&1 &
python3 __ROOT__/deepstream_safety/dashboard_server.py >__RUNTIME__/logs/dashboard.log 2>&1 &

cleanup() {
  pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/dashboard_server.py' 2>/dev/null || true
  pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/dashboard_server.py' 2>/dev/null || true
  pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/multi_runner.py' 2>/dev/null || true
  pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/multi_runner.py' 2>/dev/null || true
  pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py' 2>/dev/null || true
  pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py' 2>/dev/null || true
  pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/face_mock' 2>/dev/null || true
  pkill -f '^/usr/bin/ffmpeg .*rtsp://127.0.0.1:8554/face_mock' 2>/dev/null || true
  pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/safety_mock' 2>/dev/null || true
  pkill -f '^/usr/bin/ffmpeg .*rtsp://127.0.0.1:8554/safety_mock' 2>/dev/null || true
  pkill -f '^/opt/camera-deepstream/mediamtx/mediamtx' 2>/dev/null || true
}
trap cleanup EXIT INT TERM

curl --retry 30 --retry-delay 1 --retry-connrefused -fsS --max-time 2 \
  http://127.0.0.1:18080/dashboard.html >/dev/null
echo 'runtime-ready dashboard=http://127.0.0.1:18080/dashboard.html'
export LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cuda_nvrtc/lib:/usr/lib/wsl/lib:/usr/local/cuda/lib64:/usr/local/lib/python3.10/dist-packages/tensorrt_libs
export NVDS_ENABLE_LATENCY_MEASUREMENT=1
export NVDS_ENABLE_COMPONENT_LATENCY_MEASUREMENT=1
python3 __ROOT__/deepstream_safety/multi_runner.py --config __ROOT__/deepstream_safety/config.yaml 2>&1 | tee -a __RUNTIME__/logs/pipeline.log
'@
        $startCommand = $startCommand.Replace('__RUNTIME__', $Runtime).Replace('__ROOT__', $Root)
        $startCommand = $startCommand -replace "`r`n", "`n"
        try {
            Invoke-WSL $startCommand
        }
        finally {
            & wsl.exe -d $Distro --user root -- bash -lc "pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/dashboard_server.py' 2>/dev/null || true; pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/dashboard_server.py' 2>/dev/null || true; pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/multi_runner.py' 2>/dev/null || true; pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/multi_runner.py' 2>/dev/null || true; pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py' 2>/dev/null || true; pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py' 2>/dev/null || true; pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/face_mock' 2>/dev/null || true; pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/face_mock' 2>/dev/null || true; pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/safety_mock' 2>/dev/null || true; pkill -f '^/usr/bin/ffmpeg .*rtsp://127.0.0.1:8554/safety_mock' 2>/dev/null || true; pkill -f '^/opt/camera-deepstream/mediamtx/mediamtx' 2>/dev/null || true"
        }
    }
    'stop' {
        $stopCommand = "pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py' 2>/dev/null || true; pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/pipeline.py' 2>/dev/null || true; pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/multi_runner.py' 2>/dev/null || true; pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/multi_runner.py' 2>/dev/null || true; pkill -f '^python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/dashboard_server.py' 2>/dev/null || true; pkill -f '^/usr/bin/python3 /mnt/d/BusinessAnalyze/Camera/deepstream_safety/dashboard_server.py' 2>/dev/null || true; pkill -f '^python3 -m http.server 18080' 2>/dev/null || true; pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/face_mock' 2>/dev/null || true; pkill -f '^/usr/bin/ffmpeg .*rtsp://127.0.0.1:8554/face_mock' 2>/dev/null || true; pkill -f '^ffmpeg .*rtsp://127.0.0.1:8554/safety_mock' 2>/dev/null || true; pkill -f '^/usr/bin/ffmpeg .*rtsp://127.0.0.1:8554/safety_mock' 2>/dev/null || true; pkill -f '^/opt/camera-deepstream/mediamtx/mediamtx' 2>/dev/null || true"
        & wsl.exe -d $Distro --user root -- bash -lc $stopCommand
        Write-Output 'stopped'
    }
    'status' {
        Invoke-WSL "pgrep -af '[m]ediamtx|[d]ashboard_server.py|[p]ython3 -m http.server 18080|[f]fmpeg.*safety_mock|[p]ipeline.py' || true; tail -n 20 $Runtime/logs/pipeline.log 2>/dev/null || true"
    }
}
