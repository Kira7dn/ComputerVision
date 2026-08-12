[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('help', 'start', 'dev-start', 'dev-restart', 'dev-logs', 'dev-stop', 'acceptance-start', 'acceptance-fault', 'acceptance-restore', 'status', 'logs', 'doctor', 'stop', 'build')]
  [string]$Command = 'help',
  [string]$ConfigFile = '',
  [string]$SourceDir = '',
  [ValidateSet('service_restart', 'tracker_restart', 'stream_disconnect', 'client_disconnect', 'spool_replay', 'media_unavailable')]
  [string]$FaultScenario = ''
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$configFile = if ([string]::IsNullOrWhiteSpace($ConfigFile)) {
  Join-Path $PSScriptRoot 'config.yaml'
} else {
  $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigFile)
}
$referenceDir = Join-Path $PSScriptRoot 'reference'
$composeFile = Join-Path $referenceDir 'docker-compose.yml'
$envFile = Join-Path $workspace '.env.local'
$runtimeDir = Join-Path $workspace '.tmp\runtime'
$composeOverride = Join-Path $runtimeDir 'compose.replay.yml'
$mediaMtxReplayConfig = Join-Path $runtimeDir 'mediamtx.replay.yml'
$stateFile = Join-Path $runtimeDir 'state.json'
$imageManifestFile = Join-Path $runtimeDir 'image.json'
$defaultImage = 'camera-frigate:0.18.0-33c00a27e-runtime3-reviewfix1-tensorrt'
$buildTimeLimitSeconds = 300

function Write-AtomicUtf8([string]$Path, [string]$Content) {
  $directory = Split-Path -Parent $Path
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  $temporary = Join-Path $directory ('.tmp-' + [IO.Path]::GetRandomFileName())
  try {
    [IO.File]::WriteAllText($temporary, $Content, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
  } finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
  }
}

function Protect-Source([string]$Value) {
  if ([string]::IsNullOrEmpty($Value)) { return $Value }
  return [regex]::Replace($Value, '(?i)(rtsps?://)([^/@\s]+)@', '$1***@')
}

function Protect-Text([string]$Text, [object[]]$Sources) {
  $result = $Text
  foreach ($source in $Sources) {
    if ($source.Raw) { $result = $result.Replace([string]$source.Raw, [string]$source.Redacted) }
  }
  return Protect-Source $result
}

function Resolve-WorkspacePath([string]$Value) {
  if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
  return [IO.Path]::GetFullPath((Join-Path $workspace $Value))
}

function Resolve-DevSourcePath([string]$Value) {
  $candidate = if ([string]::IsNullOrWhiteSpace($Value)) { 'frigate/src' } else { $Value }
  $resolved = Resolve-WorkspacePath $candidate
  if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
    throw "Missing Frigate development source directory: $resolved"
  }
  if (-not (Test-Path -LiteralPath (Join-Path $resolved 'frigate\__init__.py') -PathType Leaf)) {
    throw "Development source must contain src/frigate/__init__.py: $resolved"
  }
  return $resolved
}

function Get-EnvFileValue([string]$Name) {
  if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { return '' }
  $prefix = $Name + '='
  $line = Get-Content -LiteralPath $envFile -Encoding utf8 |
    Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
    Select-Object -Last 1
  if ($null -eq $line) { return '' }
  return $line.Substring($prefix.Length).Trim().Trim('"').Trim("'")
}

function Stop-NativeProcessTree([Diagnostics.Process]$Process) {
  if ($Process.HasExited) { return }
  try {
    $Process.Kill($true)
  } catch {
    # Process.Kill(bool) is unavailable on Windows PowerShell 5.1.
    & taskkill.exe /PID $Process.Id /T /F *> $null
    if (-not $Process.HasExited) { $Process.Kill() }
  }
}

function Get-CameraConfig {
  if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) {
    throw "Missing runtime config: $configFile"
  }
  $workspacePython = Join-Path $workspace '.venv\Scripts\python.exe'
  $pythonExecutable = if (Test-Path -LiteralPath $workspacePython -PathType Leaf) {
    $workspacePython
  } else {
    (Get-Command python -ErrorAction SilentlyContinue).Source
  }
  if ([string]::IsNullOrWhiteSpace($pythonExecutable)) {
    throw 'Python 3 with PyYAML is required to read config.yaml.'
  }
  $python = "import json,sys,yaml; value=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(json.dumps(value,ensure_ascii=False))"
  $json = & $pythonExecutable -c $python $configFile 2>&1
  if ($LASTEXITCODE -ne 0) { throw "Invalid config.yaml: $($json -join ' ')" }
  $value = ($json -join "`n") | ConvertFrom-Json
  if ($null -eq $value -or $value -isnot [pscustomobject]) { throw 'config.yaml must contain a YAML mapping.' }
  return $value
}

function Get-Value($Object, [string]$Name, $Default) {
  if ($null -ne $Object -and $Object.PSObject.Properties.Name -contains $Name -and $null -ne $Object.$Name) {
    return $Object.$Name
  }
  return $Default
}

function Get-Runtime($Config) {
  if ($null -eq $Config.runtime) { throw 'config.yaml must define runtime.' }
  $runtime = $Config.runtime
  $cpu = [double](Get-Value $runtime 'cpu_limit' 4)
  if ($cpu -le 0) { throw 'runtime.cpu_limit must be greater than 0.' }
  $transport = [string](Get-Value $runtime 'rtsp_transport' 'tcp')
  if ($transport -notin @('tcp', 'udp')) { throw 'runtime.rtsp_transport must be tcp or udp.' }
  $configuredImage = [string](Get-Value $runtime 'image' $defaultImage)
  $image = $configuredImage
  $manifest = $null
  if (Test-Path -LiteralPath $imageManifestFile) {
    try {
      $manifest = Get-Content -LiteralPath $imageManifestFile -Encoding utf8 -Raw | ConvertFrom-Json
      if ($manifest.source_image -eq $configuredImage -and $manifest.image) {
        $image = [string]$manifest.image
      }
    } catch { }
  }
  return [pscustomobject]@{
    Image = $image
    ConfiguredImage = $configuredImage
    BuildBaseImage = [string](Get-Value $runtime 'build_base_image' 'camera-frigate:0.18.0-33c00a27e-runtime3-tensorrt')
    RecognitionImage = if ($manifest -and $manifest.recognition_image) { [string]$manifest.recognition_image } else { 'camera-recognition:current' }
    TrackerImage = if ($manifest -and $manifest.tracker_image) { [string]$manifest.tracker_image } else { 'camera-tracker:current' }
    # Set from the compiled topology manifest before any runtime action.
    ExternalRecognition = $false
    CpuLimit = $cpu
    ModelPath = Resolve-WorkspacePath ([string](Get-Value $runtime 'model_path' 'assets/models/yolov9-t-320.onnx'))
    MediaDir = Resolve-WorkspacePath ([string](Get-Value $runtime 'media_dir' 'E:/Docker/Frigate/media'))
    Transport = $transport
    ReplayLoop = [bool](Get-Value $runtime.replay 'loop' $true)
    ReplaySources = Get-Value $runtime.replay 'sources' ([pscustomobject]@{})
    DirectSources = Get-Value $runtime.direct 'sources' ([pscustomobject]@{})
  }
}

function Initialize-PlatformTopology {
  $workspacePython = Join-Path $workspace '.venv\Scripts\python.exe'
  if (-not (Test-Path -LiteralPath $workspacePython -PathType Leaf)) {
    throw 'Workspace Python is required to compile the platform topology.'
  }
  $compiler = Join-Path $workspace 'tools\runtime\compile_platform_topology.py'
  $topologyManifestPath = Join-Path $runtimeDir 'platform-topology.json'
  $output = & $workspacePython $compiler --config $configFile --output-dir $runtimeDir --env-file $envFile 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to compile platform topology: $($output -join ' ')"
  }
  if (-not (Test-Path -LiteralPath $topologyManifestPath -PathType Leaf)) {
    throw 'Topology compiler did not write platform-topology.json.'
  }
  $manifest = Get-Content -LiteralPath $topologyManifestPath -Encoding utf8 -Raw | ConvertFrom-Json
  $env:CAMERA_CONFIG_FILE = ([string]$manifest.main_config).Replace('\','/')
  $nodes = @()
  foreach ($node in @($manifest.nodes)) {
    $nodeId = [string]$node.id
    $nodes += [pscustomobject]@{
      Id = $nodeId
      Service = [string]$node.service
      Container = [string]$node.container
      Cameras = @($node.cameras | ForEach-Object { [string]$_ })
      TlsRoot = Join-Path $runtimeDir ("tracker-tls\" + $nodeId)
      ServerName = [string]$node.server_name
      ConfigPath = [string]$node.config_path
    }
  }
  return [pscustomobject]@{ Manifest=$manifest; Nodes=$nodes }
}

function Get-FirstStream($Streams, [string]$Name) {
  if ($null -eq $Streams -or $Streams.PSObject.Properties.Name -notcontains $Name) {
    throw "go2rtc.streams.$Name is required."
  }
  $value = $Streams.$Name
  if ($value -is [array]) { return [string]$value[0] }
  return [string]$value
}

function Resolve-CameraSources($Config, $Runtime) {
  if ($null -eq $Config.cameras -or @($Config.cameras.PSObject.Properties).Count -eq 0) {
    throw 'config.yaml must define at least one camera.'
  }
  $streams = $Config.go2rtc.streams
  $replayNames = @($Runtime.ReplaySources.PSObject.Properties.Name | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  $directNames = @($Runtime.DirectSources.PSObject.Properties.Name | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
  $overlap = @($replayNames | Where-Object { $directNames -contains $_ })
  if ($overlap.Count -gt 0) {
    throw "Camera sources cannot be both replay and direct: $($overlap -join ', ')"
  }
  foreach ($name in $replayNames) {
    if ($Config.cameras.PSObject.Properties.Name -notcontains $name) {
      throw "runtime.replay.sources.$name has no matching cameras.$name entry."
    }
  }
  $sources = @()
  foreach ($camera in $Config.cameras.PSObject.Properties) {
    $name = $camera.Name
    if ($name -notmatch '^[A-Za-z0-9_-]+$') { throw "Invalid camera name '$name'." }
    if ($directNames -contains $name) {
      $path = Resolve-WorkspacePath ([string]$Runtime.DirectSources.$name)
      if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Direct file for $name does not exist: $path" }
      $sources += [pscustomobject]@{ Name=$name; Mode='direct'; Raw=$path; Path=$path; ContainerPath="/runtime-input/$name.mp4"; Redacted=$path }
    } elseif ($replayNames -contains $name) {
      $stream = Get-FirstStream $streams $name
      $path = Resolve-WorkspacePath ([string]$Runtime.ReplaySources.$name)
      if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Replay file for $name does not exist: $path" }
      $expected = "rtsp://mediamtx:18554/$name"
      if ($stream -ne $expected) { throw "go2rtc.streams.$name must be '$expected' for a replay source." }
      $sources += [pscustomobject]@{ Name=$name; Mode='replay'; Raw=$path; Path=$path; Redacted=$path }
    } else {
      $stream = Get-FirstStream $streams $name
      if ($stream -notmatch '^(?i)rtsps?://') { throw "go2rtc.streams.$name must be RTSP or be declared under runtime.replay.sources." }
      $sources += [pscustomobject]@{ Name=$name; Mode='rtsp'; Raw=$stream; Path=$null; Redacted=(Protect-Source $stream) }
    }
  }
  return $sources
}

function ConvertTo-NativeArgument([string]$Argument) {
  if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') { return $Argument }
  $escaped = [regex]::Replace($Argument, '(\\*)"', '$1$1\"')
  $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
  return '"' + $escaped + '"'
}

function Invoke-NativeCapture(
  [string]$FilePath,
  [string[]]$Arguments,
  [int]$TimeoutSeconds,
  [object[]]$Sources,
  [string]$WorkingDirectory = ''
) {
  $process = $null
  try {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
      $startInfo.WorkingDirectory = $WorkingDirectory
    }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Unable to start $FilePath." }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      Stop-NativeProcessTree $process
      throw "$FilePath timed out after $TimeoutSeconds seconds."
    }
    $out = $stdoutTask.GetAwaiter().GetResult()
    $err = $stderrTask.GetAwaiter().GetResult()
    return [pscustomobject]@{ ExitCode=$process.ExitCode; StdOut=(Protect-Text $out $Sources); StdErr=(Protect-Text $err $Sources) }
  } finally {
    if ($null -ne $process) { $process.Dispose() }
  }
}

function Invoke-BuildStep(
  [string]$Name,
  [string]$FilePath,
  [string[]]$Arguments,
  [Diagnostics.Stopwatch]$Stopwatch,
  [string]$WorkingDirectory = ''
) {
  $remaining = $buildTimeLimitSeconds - [int][Math]::Ceiling($Stopwatch.Elapsed.TotalSeconds)
  if ($remaining -le 0) { throw "Build stopped: the $buildTimeLimitSeconds-second safety limit was reached before $Name." }
  Write-Host "[$Name] time remaining: ${remaining}s"
  $result = Invoke-NativeCapture $FilePath $Arguments $remaining @() $WorkingDirectory
  if (-not [string]::IsNullOrWhiteSpace($result.StdOut)) { Write-Host $result.StdOut.TrimEnd() }
  if (-not [string]::IsNullOrWhiteSpace($result.StdErr)) { Write-Host $result.StdErr.TrimEnd() }
  if ($result.ExitCode -ne 0) { throw "$Name failed with exit code $($result.ExitCode)." }
}

function Assert-OverlayDockerfile([string]$Path) {
  $meaningful = @(Get-Content -LiteralPath $Path -Encoding utf8 | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
  $from = @($meaningful | Where-Object { $_ -match '^(?i)FROM\s+' })
  $forbidden = @($meaningful | Where-Object { $_ -match '^(?i)(RUN|ADD)\s+' })
  if ($from.Count -ne 1 -or $from[0] -ne 'FROM ${BASE_IMAGE}' -or $forbidden.Count -gt 0) {
    throw 'Dockerfile.runtime is not a safe overlay build. Full/dependency builds are prohibited by deploy/run.ps1.'
  }
}

function Test-Sources([object[]]$Sources, [string]$Transport) {
  foreach ($tool in @('ffprobe','ffmpeg')) { if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is required." } }
  $results = @()
  $probeCache = @{}
  foreach ($source in $Sources) {
    $cacheKey = "$($source.Mode)|$($source.Raw)"
    if ($probeCache.ContainsKey($cacheKey)) {
      $cached = $probeCache[$cacheKey]
      $results += [pscustomobject]@{ Name=$source.Name; Codec=$cached.Codec; Width=$cached.Width; Height=$cached.Height }
      continue
    }
    $input = @(); if ($source.Mode -eq 'rtsp') { $input += @('-rtsp_transport',$Transport) }
    $probe = Invoke-NativeCapture 'ffprobe' (@('-v','error') + $input + @('-select_streams','v:0','-show_entries','stream=codec_name,width,height','-of','json',$source.Raw)) 15 $Sources
    if ($probe.ExitCode -ne 0) { throw "Source '$($source.Name)' probe failed: $($probe.StdErr.Trim())" }
    $video = @(($probe.StdOut | ConvertFrom-Json).streams)[0]
    if ($null -eq $video) { throw "Source '$($source.Name)' has no video stream." }
    if ([string]$video.codec_name -ne 'h264') { throw "Source '$($source.Name)' uses '$($video.codec_name)'; only H.264 is supported." }
    $decode = Invoke-NativeCapture 'ffmpeg' (@('-v','error') + $input + @('-i',$source.Raw,'-map','0:v:0','-frames:v','1','-f','null','NUL')) 20 $Sources
    if ($decode.ExitCode -ne 0) { throw "Source '$($source.Name)' opened but no frame could be decoded: $($decode.StdErr.Trim())" }
    $result = [pscustomobject]@{ Name=$source.Name; Codec='h264'; Width=[int]$video.width; Height=[int]$video.height }
    $probeCache[$cacheKey] = $result
    $results += $result
  }
  return $results
}

function Set-ComposeEnvironment($Runtime) {
  New-Item -ItemType Directory -Force -Path $Runtime.MediaDir | Out-Null
  $env:FRIGATE_IMAGE = $Runtime.Image
  $env:RECOGNITION_IMAGE = $Runtime.RecognitionImage
  $env:TRACKER_IMAGE = $Runtime.TrackerImage
  $env:FRIGATE_CPU_LIMIT = [string]$Runtime.CpuLimit
  $env:CAMERA_MODEL_PATH = $Runtime.ModelPath.Replace('\','/')
  $env:FRIGATE_MEDIA_DIR = $Runtime.MediaDir.Replace('\','/')
  if ([string]::IsNullOrWhiteSpace($env:RECOGNITION_TLS_DIR)) {
    $env:RECOGNITION_TLS_DIR = (Join-Path $runtimeDir 'recognition-tls').Replace('\','/')
  }
  $env:NGROK_URL = Get-EnvFileValue 'NGROK_URL'
  foreach ($mapping in @(
    @('FRIGATE_TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN'),
    @('FRIGATE_TELEGRAM_CHAT_ID', 'TELEGRAM_CHAT_ID'),
    @('FRIGATE_ZALO_BOT_TOKEN', 'ZALO_BOT_TOKEN'),
    @('FRIGATE_ZALO_CHAT_ID', 'ZALO_CHAT_ID')
  )) {
    $current = [Environment]::GetEnvironmentVariable($mapping[0])
    if ([string]::IsNullOrWhiteSpace($current)) {
      $legacy = Get-EnvFileValue $mapping[1]
      if (-not [string]::IsNullOrWhiteSpace($legacy)) {
        [Environment]::SetEnvironmentVariable($mapping[0], $legacy)
      }
    }
  }
}

function Test-NgrokConfiguration($Config) {
  $token = Get-EnvFileValue 'NGROK_AUTHTOKEN'
  $url = (Get-EnvFileValue 'NGROK_URL').TrimEnd('/')
  if ([string]::IsNullOrWhiteSpace($token)) { throw 'NGROK_AUTHTOKEN is required in .env.local.' }
  if ($url -notmatch '^https://[A-Za-z0-9.-]+(?::\d+)?$') { throw 'NGROK_URL must be a reserved HTTPS origin in .env.local.' }
  $configured = [string](Get-Value $Config.notifications 'public_base_url' '')
  if ($configured -and $configured -ne '{NGROK_URL}' -and $configured.TrimEnd('/') -ne $url) {
    throw 'notifications.public_base_url must match NGROK_URL.'
  }
  return $url
}

function Get-NgrokTunnelUrl {
  $oldPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = & docker exec frigate curl --fail --silent --max-time 2 http://camera-ngrok:4040/api/tunnels 2>$null
  } finally {
    $ErrorActionPreference = $oldPreference
  }
  if ($LASTEXITCODE -ne 0 -or -not $output) { return '' }
  try {
    $document = ($output -join "`n") | ConvertFrom-Json
    return [string](@($document.tunnels | Where-Object { $_.proto -eq 'https' } | Select-Object -First 1).public_url)
  } catch { return '' }
}

function Wait-NgrokReady([string]$ExpectedUrl) {
  $deadline = [DateTime]::UtcNow.AddSeconds(120)
  do {
    $actual = (Get-NgrokTunnelUrl).TrimEnd('/')
    if ($actual -eq $ExpectedUrl) {
      $expires = [DateTimeOffset]::UtcNow.AddMinutes(1).ToUnixTimeSeconds()
      $probe = "$ExpectedUrl/api/notifications/media/readiness/artifact.jpg?expires=$expires&signature=invalid"
      $oldPreference = $ErrorActionPreference
      try {
        $ErrorActionPreference = 'Continue'
        $status = & docker exec frigate curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 $probe 2>$null
      } finally {
        $ErrorActionPreference = $oldPreference
      }
      if ($LASTEXITCODE -eq 0 -and [string]$status -eq '403') {
        Write-Host "ngrok ready at $ExpectedUrl; signed media route rejects invalid signatures."
        return $true
      }
    }
    $ngrokState = & docker inspect camera-ngrok --format '{{.State.Status}}' 2>$null
    if ($ngrokState -in @('restarting','exited','dead')) { break }
    Start-Sleep -Seconds 2
  } while ([DateTime]::UtcNow -lt $deadline)
  Write-Warning 'ngrok tunnel is degraded; Telegram media remains available but public media actions, Zalo, and WebPush are disabled.'
  return $false
}

function New-ReplayOverride([object[]]$Sources, $Runtime, [bool]$NotificationsEnabled, [object[]]$TrackerNodes) {
  $lines = [Collections.Generic.List[string]]::new()
  $lines.Add('services:')
  $frigateVolumes = [Collections.Generic.List[string]]::new()
  if (-not [string]::IsNullOrWhiteSpace($env:CAMERA_SOURCE_OVERLAY)) {
    $sourceOverlay = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($env:CAMERA_SOURCE_OVERLAY)
    if (-not (Test-Path -LiteralPath $sourceOverlay -PathType Container)) {
      throw "Missing CAMERA_SOURCE_OVERLAY directory: $sourceOverlay"
    }
    $sourceMount = $sourceOverlay.Replace('\','/') + ':/opt/frigate/src/frigate:ro'
    $frigateVolumes.Add($sourceMount)
  }
  if (-not [string]::IsNullOrWhiteSpace($env:CAMERA_REPORT_MEDIA_DIR)) {
    $reportMedia = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($env:CAMERA_REPORT_MEDIA_DIR)
    if (-not (Test-Path -LiteralPath $reportMedia -PathType Container)) {
      throw "Missing CAMERA_REPORT_MEDIA_DIR directory: $reportMedia"
    }
    $frigateVolumes.Add($reportMedia.Replace('\','/') + ':/runtime-evidence')
  }
  foreach ($source in @($Sources | Where-Object Mode -eq 'direct')) {
    $frigateVolumes.Add($source.Path.Replace('\','/') + ":$($source.ContainerPath):ro")
  }
  foreach ($node in $TrackerNodes) {
    $frigateVolumes.Add(
      $node.TlsRoot.Replace('\','/') + ":/run/tracker-tls/$($node.Id):ro"
    )
  }
  if ($frigateVolumes.Count -gt 0) {
    $mounts = @($frigateVolumes | ForEach-Object { $_ | ConvertTo-Json -Compress }) -join ', '
    $lines.Add('  frigate:')
    $lines.Add("    volumes: [$mounts]")
  }
  if (-not $NotificationsEnabled) {
    $lines.Add('  ngrok:')
    $lines.Add('    profiles: ["notifications"]')
  }
  $replays = @($Sources | Where-Object Mode -eq 'replay')
  if ($replays.Count -eq 0 -and $frigateVolumes.Count -eq 0 -and $NotificationsEnabled) { $lines.Add('  {}') }
  if ($replays.Count -gt 0) {
    $mediaMount = (($mediaMtxReplayConfig.Replace('\','/') + ':/mediamtx.yml:ro') | ConvertTo-Json -Compress)
    $lines.Add('  mediamtx:')
    $lines.Add("    volumes: [$mediaMount]")
  }
  $groups = @($replays | Group-Object Path)
  foreach ($group in $groups) {
    $source = @($group.Group)[0]
    $service = 'replay-' + $source.Name.ToLowerInvariant().Replace('_','-')
    $container = 'camera-replay-' + $source.Name.ToLowerInvariant().Replace('_','-')
    $volume = (($source.Path.Replace('\','/') + ':/runtime/source:ro') | ConvertTo-Json -Compress)
    $loop = if ($Runtime.ReplayLoop) { '-1' } else { '0' }
    $lines.Add("  ${service}:")
    $lines.Add('    image: ${FRIGATE_IMAGE}')
    $lines.Add("    container_name: $container")
    $lines.Add('    profiles: ["replay"]')
    $lines.Add('    restart: unless-stopped')
    $lines.Add('    healthcheck: { disable: true }')
    $lines.Add('    depends_on: [mediamtx]')
    $lines.Add('    entrypoint: ["/usr/lib/ffmpeg/7.0/bin/ffmpeg"]')
    $lines.Add("    volumes: [$volume]")
      # Frequent IDR frames let Frigate/go2rtc attach immediately after a
      # publisher restart instead of waiting on a long source GOP.
      $commandArgs = @('-hide_banner','-loglevel','warning','-re','-stream_loop',$loop,'-fflags','+genpts','-i','/runtime/source','-map','0:v:0','-an','-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-g','15','-keyint_min','15','-sc_threshold','0','-x264-params','repeat-headers=1','-f','rtsp','-rtsp_transport','tcp',("rtsp://mediamtx:18554/$($source.Name)"))
    $command = $commandArgs | ConvertTo-Json -Compress
    $lines.Add("    command: $command")
  }
  foreach ($node in $TrackerNodes) {
    $configMount = (($node.ConfigPath.Replace('\','/') + ':/config/config.yml:ro') | ConvertTo-Json -Compress)
    $modelMount = (($Runtime.ModelPath.Replace('\','/') + ':/assets/models/yolov9-t-320.onnx:ro') | ConvertTo-Json -Compress)
    $tlsMount = (($node.TlsRoot.Replace('\','/') + ':/run/tracker-tls:ro') | ConvertTo-Json -Compress)
    $trackerVolumes = [Collections.Generic.List[string]]::new()
    $trackerVolumes.Add($configMount)
    $trackerVolumes.Add($modelMount)
    $trackerVolumes.Add($tlsMount)
    if (-not [string]::IsNullOrWhiteSpace($env:CAMERA_SOURCE_OVERLAY)) {
      $trackerVolumes.Add((($sourceOverlay.Replace('\','/') + ':/opt/frigate/src/frigate:ro') | ConvertTo-Json -Compress))
    }
    foreach ($source in @($Sources | Where-Object { $_.Mode -eq 'direct' -and $node.Cameras -contains $_.Name })) {
      $trackerVolumes.Add((($source.Path.Replace('\','/') + ":$($source.ContainerPath):ro") | ConvertTo-Json -Compress))
    }
    if (-not [string]::IsNullOrWhiteSpace($env:CAMERA_REPORT_MEDIA_DIR)) {
      $trackerVolumes.Add((($reportMedia.Replace('\','/') + ':/runtime-evidence') | ConvertTo-Json -Compress))
    }
    $volumeList = @($trackerVolumes) -join ', '
    $mediaVolume = 'camera-tracker-' + $node.Id.ToLowerInvariant().Replace('_','-') + '-media'
    $spoolVolume = 'camera-tracker-' + $node.Id.ToLowerInvariant().Replace('_','-') + '-spool'
    $lines.Add("  $($node.Service):")
    $lines.Add('    image: ${TRACKER_IMAGE}')
    $lines.Add("    container_name: $($node.Container)")
    $lines.Add('    profiles: ["external-tracker"]')
    $lines.Add('    restart: unless-stopped')
    $lines.Add('    shm_size: "1gb"')
    $lines.Add('    gpus: all')
    $lines.Add('    env_file:')
    $lines.Add('      - path: ../../.env.local')
    $lines.Add('        required: true')
    $lines.Add('    environment:')
    $lines.Add("      TRACKER_NODE_ID: $($node.Id)")
    $lines.Add('      FRIGATE_TELEGRAM_BOT_TOKEN: ${FRIGATE_TELEGRAM_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}')
    $lines.Add('      FRIGATE_TELEGRAM_CHAT_ID: ${FRIGATE_TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}')
    $lines.Add('      FRIGATE_ZALO_BOT_TOKEN: ${FRIGATE_ZALO_BOT_TOKEN:-${ZALO_BOT_TOKEN:-}}')
    $lines.Add('      FRIGATE_ZALO_CHAT_ID: ${FRIGATE_ZALO_CHAT_ID:-${ZALO_CHAT_ID:-}}')
    $lines.Add('      PASSAGE_TRACE_PATH: ${PASSAGE_TRACE_PATH:-}')
    $lines.Add('      PASSAGE_EVIDENCE_DIR: ${PASSAGE_EVIDENCE_DIR:-}')
    $lines.Add('      PASSAGE_EVIDENCE_MAX_BYTES: ${PASSAGE_EVIDENCE_MAX_BYTES:-134217728}')
    $lines.Add('      PASSAGE_EVIDENCE_MAX_RECORDS: ${PASSAGE_EVIDENCE_MAX_RECORDS:-4096}')
    $lines.Add('      PASSAGE_CAPTURE_START_PATH: ${PASSAGE_CAPTURE_START_PATH:-}')
    $lines.Add('      PASSAGE_CAPTURE_CUTOFF_PATH: ${PASSAGE_CAPTURE_CUTOFF_PATH:-}')
    $lines.Add('      PASSAGE_RUN_ID: ${PASSAGE_RUN_ID:-}')
    $lines.Add('      PASSAGE_SOURCE_START_DIR: ${PASSAGE_SOURCE_START_DIR:-}')
    $lines.Add("    volumes: [$volumeList, ${mediaVolume}:/media/frigate, ${spoolVolume}:/var/lib/camera-tracker/spool]")
  }
  if ($TrackerNodes.Count -gt 0) {
    $lines.Add('volumes:')
    foreach ($node in $TrackerNodes) {
      $suffix = $node.Id.ToLowerInvariant().Replace('_','-')
      $lines.Add("  camera-tracker-${suffix}-media: {}")
      $lines.Add("  camera-tracker-${suffix}-spool: {}")
    }
  }
  Write-AtomicUtf8 $composeOverride (($lines -join "`n") + "`n")

  $mediaLines = [Collections.Generic.List[string]]::new()
  @(
    'logLevel: info',
    'rtsp: true',
    'rtspAddress: :18554',
    'rtspTransports: [tcp]',
    'hls: false',
    'webrtc: false',
    'api: true',
    'apiAddress: :9997',
    'paths:'
  ) | ForEach-Object { $mediaLines.Add($_) }
  foreach ($group in $groups) {
    $members = @($group.Group)
    $publisher = $members[0].Name
    $mediaLines.Add("  ${publisher}:")
    $mediaLines.Add('    source: publisher')
    foreach ($alias in @($members | Select-Object -Skip 1)) {
      $mediaLines.Add("  $($alias.Name):")
      $mediaLines.Add("    source: rtsp://127.0.0.1:18554/${publisher}")
    }
  }
  if ($groups.Count -eq 0) {
    $mediaLines.Add('  all_others:')
    $mediaLines.Add('    source: publisher')
  }
  Write-AtomicUtf8 $mediaMtxReplayConfig (($mediaLines -join "`n") + "`n")
}

function Get-ComposePrefix([bool]$Replay, [bool]$ExternalRecognition, [bool]$ExternalTracker) {
  if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw "Missing required secrets file: $envFile" }
  $args = @('compose','-f',$composeFile,'-f',$composeOverride,'--env-file',$envFile)
  if ($Replay) { $args += @('--profile','replay') }
  if ($ExternalRecognition) { $args += @('--profile','external-recognition') }
  if ($ExternalTracker) { $args += @('--profile','external-tracker') }
  return $args
}

function Invoke-Compose([string[]]$Prefix, [string[]]$Arguments) {
  $savedErrorActionPreference = $ErrorActionPreference
  try {
    # Docker Compose writes progress and warnings to stderr even on success.
    # Native exit status, not the stderr stream, is the command contract.
    $ErrorActionPreference = 'Continue'
    & docker @Prefix @Arguments
    $composeExitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $savedErrorActionPreference
  }
  if ($composeExitCode -ne 0) { throw "Docker Compose failed with exit code $composeExitCode." }
}

function Wait-ReplayReady([object[]]$ReplaySources, [int]$TimeoutSeconds = 30) {
  if ($ReplaySources.Count -eq 0) { return }
  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  $lastReason = ''
  while ([DateTime]::UtcNow -lt $deadline) {
    $mediaRunning = ((& docker inspect camera-mediamtx --format '{{.State.Running}}' 2>$null) -join '').Trim()
    if ($mediaRunning -ne 'true') {
      $lastReason = "camera-mediamtx running=$mediaRunning"
      Start-Sleep -Milliseconds 250
      continue
    }
    $allPublishersReady = $true
    foreach ($source in $ReplaySources) {
      $container = 'camera-replay-' + $source.Name.ToLowerInvariant().Replace('_','-')
      $running = ((& docker inspect $container --format '{{.State.Running}}' 2>$null) -join '').Trim()
      if ($running -ne 'true') {
        $allPublishersReady = $false
        $lastReason = "$container running=$running"
        break
      }
    }
    if ($allPublishersReady) {
      # Docker's embedded DNS only advertises a usable service while the
      # target container is attached/running.  Verify that condition before
      # Frigate opens go2rtc/FFmpeg readers.
      $network = ((& docker inspect camera-mediamtx --format '{{json .NetworkSettings.Networks}}' 2>$null) -join '').Trim()
      if ($network -and $network -ne '{}') { return }
      $lastReason = 'camera-mediamtx has no attached Docker network'
    }
    Start-Sleep -Milliseconds 250
  }
  throw "Replay topology did not become ready before Frigate startup: $lastReason"
}

function Test-RuntimeDependencies($Runtime) {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI is required.' }
  & docker info *> $null; if ($LASTEXITCODE -ne 0) { throw 'Docker Engine is unavailable.' }
  $dockerCpuCount = [int]((& docker info --format '{{.NCPU}}').Trim())
  if ($LASTEXITCODE -ne 0 -or $dockerCpuCount -le 0) { throw 'Unable to determine Docker CPU capacity.' }
  if ([double]$Runtime.CpuLimit -gt $dockerCpuCount) {
    throw "runtime.cpu_limit ($($Runtime.CpuLimit)) exceeds Docker CPU capacity ($dockerCpuCount)."
  }
  & docker image inspect $Runtime.Image *> $null; if ($LASTEXITCODE -ne 0) { throw "Missing runtime image: $($Runtime.Image)" }
  if ($Runtime.ExternalRecognition) {
    & docker image inspect $Runtime.RecognitionImage *> $null
    if ($LASTEXITCODE -ne 0) { throw "Missing recognition image: $($Runtime.RecognitionImage)" }
    foreach ($name in @('ca.crt','server.crt','server.key','client.crt','client.key')) {
      $path = Join-Path $env:RECOGNITION_TLS_DIR $name
      if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing recognition TLS file: $path" }
    }
  }
  if (-not (Test-Path -LiteralPath $Runtime.ModelPath -PathType Leaf)) { throw "Missing model: $($Runtime.ModelPath)" }
}

function Test-TrackerDependencies($Runtime, [object[]]$TrackerNodes) {
  if ($TrackerNodes.Count -eq 0) { return }
  & docker image inspect $Runtime.TrackerImage *> $null
  if ($LASTEXITCODE -ne 0) { throw "Missing tracker image: $($Runtime.TrackerImage)" }
  foreach ($node in $TrackerNodes) {
    foreach ($name in @('ca.crt','server.crt','server.key','client.crt','client.key')) {
      $path = Join-Path $node.TlsRoot $name
      if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing tracker TLS file: $path" }
    }
  }
}

function Wait-RecognitionReady([int]$TimeoutSeconds = 60) {
  $probe = @'
import grpc
from pathlib import Path
from frigate.recognition.service import health_pb2, health_pb2_grpc
root = Path('/run/recognition-tls')
credentials = grpc.ssl_channel_credentials(
    root_certificates=(root / 'ca.crt').read_bytes(),
    private_key=(root / 'client.key').read_bytes(),
    certificate_chain=(root / 'client.crt').read_bytes(),
)
try:
    channel = grpc.secure_channel(
        '127.0.0.1:50051', credentials,
        options=(('grpc.ssl_target_name_override', 'recognition'),),
    )
    response = health_pb2_grpc.HealthStub(channel).Check(
        health_pb2.HealthCheckRequest(service='camera.recognition.v1.RecognitionService'),
        timeout=2,
    )
except Exception:
    raise SystemExit(1) from None
raise SystemExit(0 if response.status == health_pb2.HealthCheckResponse.SERVING else 1)
'@
  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  $savedErrorActionPreference = $ErrorActionPreference
  try {
    # Connection failures are expected while models and the gRPC server start.
    # Treat them as a failed poll instead of terminating the deployment script.
    $ErrorActionPreference = 'SilentlyContinue'
    do {
      $running = (& docker inspect camera-recognition --format '{{.State.Running}}' 2>$null).Trim()
      if ($running -eq 'true') {
        & docker exec camera-recognition python3 -c $probe *> $null
        if ($LASTEXITCODE -eq 0) { return }
      }
      Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
  } finally {
    $ErrorActionPreference = $savedErrorActionPreference
  }
  $logs = (& docker logs --tail 80 camera-recognition 2>&1) -join "`n"
  throw "Recognition service did not become healthy: $logs"
}

function Wait-TrackerReady([object[]]$TrackerNodes, [int]$TimeoutSeconds = 90) {
  if ($TrackerNodes.Count -eq 0) { return @() }
  $states = @()
  $savedErrorActionPreference = $ErrorActionPreference
  try {
    # Connection failures are expected while the tracker imports the shared
    # Frigate runtime and starts its camera workers. Treat them as failed polls.
    $ErrorActionPreference = 'SilentlyContinue'
    foreach ($node in $TrackerNodes) {
      $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
      $cameraJson = ConvertTo-Json -InputObject @($node.Cameras) -Compress
      $probe = @"
import grpc, json
from pathlib import Path
from camera_platform.tracker.service.v1 import tracker_pb2 as pb, tracker_pb2_grpc as rpc
root = Path('/run/tracker-tls')
credentials = grpc.ssl_channel_credentials(
    root_certificates=(root / 'ca.crt').read_bytes(),
    private_key=(root / 'client.key').read_bytes(),
    certificate_chain=(root / 'client.crt').read_bytes(),
)
channel = grpc.secure_channel(
    '127.0.0.1:50052', credentials,
    options=(('grpc.ssl_target_name_override', '$($node.ServerName)'),),
)
response = rpc.TrackerServiceStub(channel).GetCapabilities(pb.CapabilitiesRequest(), timeout=2)
expected = set(json.loads('$cameraJson'))
actual = {item.camera_id for item in response.cameras if item.ready}
valid = response.node_id == '$($node.Id)' and response.schema_version == '1.0.0' and response.mtls_required and expected == actual
if valid:
    print(json.dumps({
        'node_id': response.node_id,
        'node_epoch': response.node_epoch,
        'schema_version': response.schema_version,
        'config_hash': response.config_hash,
        'mtls_required': response.mtls_required,
        'health': {
            'ready': response.health.ready,
            'degraded': response.health.degraded,
            'pending_ack': response.health.pending_ack,
            'spool_bytes': response.health.spool_bytes,
            'pinned_evidence': response.health.pinned_evidence,
            'active_lifecycles': response.health.active_lifecycles,
        },
        'cameras': [
            {
                'camera_id': item.camera_id,
                'ready': item.ready,
                'camera_fps': item.camera_fps,
                'process_fps': item.process_fps,
                'capture_pid': item.capture_pid,
                'process_pid': item.process_pid,
            }
            for item in response.cameras
        ],
    }, sort_keys=True))
raise SystemExit(0 if valid else 1)
"@
      $ready = $false
      do {
        $running = ((& docker inspect $node.Container --format '{{.State.Running}}' 2>$null) -join '').Trim()
        if ($running -eq 'true') {
          $probeOutput = (($probe | & docker exec -i $node.Container python3 - 2>$null) -join '').Trim()
          if ($LASTEXITCODE -eq 0) {
            $ready = $true
            $states += ($probeOutput | ConvertFrom-Json)
            break
          }
        }
        Start-Sleep -Milliseconds 500
      } while ([DateTime]::UtcNow -lt $deadline)
      if (-not $ready) {
        $logs = (& docker logs --tail 100 $node.Container 2>&1) -join "`n"
        throw "Tracker node '$($node.Id)' did not reach camera readiness: $logs"
      }
    }
  } finally {
    $ErrorActionPreference = $savedErrorActionPreference
  }
  return $states
}

function Ensure-FrigateConfigVolume {
  $volumeName = 'camera-frigate-config'
  $existingVolumes = @(& docker volume ls --filter "name=^${volumeName}$" --format '{{.Name}}')
  if ($existingVolumes -notcontains $volumeName) {
    & docker volume create $volumeName *> $null
    if ($LASTEXITCODE -ne 0) { throw "Unable to create Docker volume: $volumeName" }
  }
}

function Build-RuntimeImage($Runtime) {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI is required.' }
  & docker image inspect $Runtime.BuildBaseImage *> $null
  if ($LASTEXITCODE -ne 0) {
    throw "Missing local build base image: $($Runtime.BuildBaseImage). Import the approved base image first; automatic full builds and pulls are disabled."
  }

  $dockerfile = Join-Path $referenceDir 'Dockerfile.runtime'
  Assert-OverlayDockerfile $dockerfile
  $stopwatch = [Diagnostics.Stopwatch]::StartNew()

  $webDir = Join-Path $workspace 'frigate\web'
  Push-Location $webDir
  try {
    if (-not (Test-Path -LiteralPath (Join-Path $webDir 'node_modules'))) {
      Invoke-BuildStep 'npm ci' 'cmd.exe' @('/d','/s','/c','npm ci') $stopwatch
    }
    Invoke-BuildStep 'frontend' 'cmd.exe' @('/d','/s','/c','npm run build') $stopwatch $webDir
  } finally {
    Pop-Location
  }

  $sourceDir = Join-Path $workspace 'frigate'
  $dockerPath = (Get-Command docker).Source
  $dockerArgs = @('buildx','build','--load','--pull=false','--file',$dockerfile,'--build-context',"webdist=$webDir\dist",'--build-arg',"BASE_IMAGE=$($Runtime.BuildBaseImage)",'--tag',$Runtime.ConfiguredImage,$sourceDir)
  Invoke-BuildStep 'runtime overlay' $dockerPath $dockerArgs $stopwatch
  $imageId = (& docker image inspect --format '{{.Id}}' $Runtime.ConfiguredImage).Trim()
  if ($LASTEXITCODE -ne 0 -or -not $imageId.StartsWith('sha256:')) {
    throw 'Unable to resolve the built image digest.'
  }
  $repository = $Runtime.ConfiguredImage.Split(':')[0]
  $immutableImage = "${repository}:overlay-$($imageId.Substring(7,12))"
  & docker tag $Runtime.ConfiguredImage $immutableImage

  $recognitionDockerfile = Join-Path $referenceDir 'Dockerfile.recognition'
  Assert-OverlayDockerfile $recognitionDockerfile
  $recognitionCurrent = 'camera-recognition:current'
  $recognitionArgs = @('buildx','build','--load','--pull=false','--file',$recognitionDockerfile,'--build-arg',"BASE_IMAGE=$immutableImage",'--tag',$recognitionCurrent,$sourceDir)
  Invoke-BuildStep 'recognition overlay' $dockerPath $recognitionArgs $stopwatch
  $recognitionId = (& docker image inspect --format '{{.Id}}' $recognitionCurrent).Trim()
  if (-not $recognitionId.StartsWith('sha256:')) { throw "Invalid recognition image id: $recognitionId" }
  $recognitionImage = "camera-recognition:overlay-$($recognitionId.Substring(7,12))"
  & docker tag $recognitionCurrent $recognitionImage
  if ($LASTEXITCODE -ne 0) { throw 'Unable to create immutable runtime image tag.' }

  $trackerDockerfile = Join-Path $referenceDir 'Dockerfile.tracker'
  $trackerCurrent = 'camera-tracker:current'
  $trackerArgs = @('buildx','build','--load','--pull=false','--file',$trackerDockerfile,'--build-context',"trackerfiles=$referenceDir",'--build-arg',"BASE_IMAGE=$immutableImage",'--tag',$trackerCurrent,$sourceDir)
  Invoke-BuildStep 'tracker overlay' $dockerPath $trackerArgs $stopwatch
  $trackerId = (& docker image inspect --format '{{.Id}}' $trackerCurrent).Trim()
  if (-not $trackerId.StartsWith('sha256:')) { throw "Invalid tracker image id: $trackerId" }
  $trackerImage = "camera-tracker:overlay-$($trackerId.Substring(7,12))"
  & docker tag $trackerCurrent $trackerImage
  if ($LASTEXITCODE -ne 0) { throw 'Unable to create immutable tracker image tag.' }
  $runtimeSize = [int64]((& docker image inspect --format '{{.Size}}' $immutableImage).Trim())
  $recognitionSize = [int64]((& docker image inspect --format '{{.Size}}' $recognitionImage).Trim())
  $trackerSize = [int64]((& docker image inspect --format '{{.Size}}' $trackerImage).Trim())
  $sourceCommit = (& git -C (Join-Path $workspace 'frigate') rev-parse HEAD).Trim()
  $worktreeHash = ((& git -C (Join-Path $workspace 'frigate') diff --binary HEAD | git hash-object --stdin) -join '').Trim()
  $manifest = [ordered]@{
    source_image = $Runtime.ConfiguredImage
    image = $immutableImage
    recognition_image = $recognitionImage
    tracker_image = $trackerImage
    digest = $imageId
    runtime_bytes = $runtimeSize
    recognition_digest = $recognitionId
    recognition_bytes = $recognitionSize
    tracker_digest = $trackerId
    tracker_bytes = $trackerSize
    source_commit = $sourceCommit
    worktree_hash = $worktreeHash
    built_at = [DateTime]::UtcNow.ToString('o')
  }
  Write-AtomicUtf8 $imageManifestFile (($manifest | ConvertTo-Json) + "`n")
  $stopwatch.Stop()
  Write-Host ("Built runtime images: {0}, {1}, {2} in {3:n1}s (all via run.ps1 overlay build)." -f $immutableImage,$recognitionImage,$trackerImage,$stopwatch.Elapsed.TotalSeconds)
}

function Test-FrigateConfig($Runtime, [object[]]$Sources) {
  $validator = "from frigate.config import FrigateConfig; f=open('/config/config.yml',encoding='utf-8'); FrigateConfig.parse(f); f.close(); print('Frigate config: OK')"
  $effectiveConfig = if ([string]::IsNullOrWhiteSpace($env:CAMERA_CONFIG_FILE)) {
    $configFile
  } else {
    [string]$env:CAMERA_CONFIG_FILE
  }
  $oldPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = & docker run --rm --entrypoint python3 `
      -e CONFIG_FILE=/config/config.yml `
      -e FRIGATE_TELEGRAM_BOT_TOKEN `
      -e FRIGATE_TELEGRAM_CHAT_ID `
      -e FRIGATE_ZALO_BOT_TOKEN `
      -e FRIGATE_ZALO_CHAT_ID `
      -e NGROK_URL `
      -v "${effectiveConfig}:/config/config.yml:ro" `
      -v "$($Runtime.ModelPath):/assets/models/yolov9-t-320.onnx:ro" `
      $Runtime.Image -c $validator 2>&1
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldPreference
  }
  if ($exitCode -ne 0) { throw (Protect-Text ($output -join "`n") $Sources) }
}

function Wait-RuntimeReady([object[]]$Sources, $Config) {
  $deadline = [DateTime]::UtcNow.AddSeconds(90)
  $stableSeconds = 10.0
  if ($env:CAMERA_READY_STABLE_SECONDS) {
    $candidate = 0.0
    if ([double]::TryParse($env:CAMERA_READY_STABLE_SECONDS, [ref]$candidate) -and $candidate -ge 1.0 -and $candidate -le 10.0) {
      $stableSeconds = $candidate
    }
  }
  $faceRequired = $null -ne $Config.face_recognition -and [bool](Get-Value $Config.face_recognition 'enabled' $false)
  $stableSince = $null
  $restartSignature = $null
  do {
    try {
      $stats = Get-FrigateInternalStats
      $ready = $true
      foreach ($source in $Sources) {
        $camera = $stats.cameras.($source.Name)
        if ($null -eq $camera -or [double]$camera.camera_fps -lt 4.5 -or [double]$camera.process_fps -lt 4.5) { $ready = $false; break }
      }
      $faceReady = -not $faceRequired -or $null -ne $stats.embeddings.face_recognition
      $detectorsReady = @($stats.detectors.PSObject.Properties.Value | Where-Object { [double]$_.inference_speed -lt 200 }).Count -eq @($stats.detectors.PSObject.Properties).Count
      if ($ready -and $faceReady -and $detectorsReady) {
        & docker exec frigate sh -c 'set -eu; test -w /config; test -w /media/frigate; touch /config/.ready-write; rm /config/.ready-write; touch /media/frigate/.ready-write; rm /media/frigate/.ready-write' *> $null
        if ($LASTEXITCODE -eq 0) {
          $runtimeContainers = @(& docker ps --format '{{.Names}}' | Where-Object { $_ -eq 'frigate' -or $_ -like 'camera-replay-*' })
          $currentSignature = (@($runtimeContainers | Sort-Object | ForEach-Object {
            $count = & docker inspect $_ --format '{{.RestartCount}}' 2>$null
            "${_}:$count"
          }) -join ',')
          if ($null -eq $stableSince -or $restartSignature -ne $currentSignature) {
            $stableSince = [DateTime]::UtcNow
            $restartSignature = $currentSignature
          } elseif (([DateTime]::UtcNow - $stableSince).TotalSeconds -ge $stableSeconds) {
            return
          }
          Start-Sleep -Milliseconds 750
          continue
        }
      }
      $stableSince = $null
    } catch { $stableSince = $null }
    Start-Sleep -Milliseconds 750
  } while ([DateTime]::UtcNow -lt $deadline)
  throw "Runtime did not remain ready without camera restarts for $stableSeconds seconds within the 90-second startup window."
}

function Get-FrigateInternalStats {
  $output = & docker exec frigate curl --fail --silent --show-error --max-time 2 http://127.0.0.1:5000/api/stats 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $output) { throw 'Frigate internal stats endpoint is unavailable.' }
  return ($output -join "`n") | ConvertFrom-Json
}

function Get-State {
  if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
  try { return Get-Content -LiteralPath $stateFile -Encoding utf8 -Raw | ConvertFrom-Json } catch { return $null }
}

function Show-Status {
  $state = Get-State
  if ($null -eq $state) { Write-Host 'No runtime state. Run deploy\run.ps1 doctor or start.'; return }
  $running = (& docker ps --format '{{.Names}}' 2>$null) -contains 'frigate'
  Write-Host "Frigate: $(if ($running) { 'running' } else { 'stopped' })"
  if ($state.development) {
    Write-Host "Development source: $($state.source_overlay) (read-only bind mount)"
  }
  foreach ($camera in $state.cameras) { Write-Host ("Camera {0}: {1} ({2})" -f $camera.name,$camera.source,$camera.mode) }
  if (-not $running) { return }
  try {
    $stats = Get-FrigateInternalStats
    foreach ($camera in $state.cameras) {
      $value = $stats.cameras.($camera.name)
      Write-Host ("{0}: camera_fps={1}, process_fps={2}, skipped_fps={3}" -f $camera.name,$value.camera_fps,$value.process_fps,$value.skipped_fps)
    }
    foreach ($detector in $stats.detectors.PSObject.Properties) {
      Write-Host ("Detector {0}: inference={1} ms" -f $detector.Name,$detector.Value.inference_speed)
    }
  } catch { Write-Warning 'Stats endpoint is unavailable.' }
}

function Show-Help {
  @'
Camera runtime

  .\deploy\run.ps1 start
  .\deploy\run.ps1 dev-start
  .\deploy\run.ps1 dev-restart
  .\deploy\run.ps1 dev-logs
  .\deploy\run.ps1 dev-stop
  .\deploy\run.ps1 status
  .\deploy\run.ps1 logs
  .\deploy\run.ps1 doctor
  .\deploy\run.ps1 stop
  .\deploy\run.ps1 build

Use -ConfigFile to select an isolated config; the default is .\deploy\config.yaml.
Development commands bind-mount -SourceDir read-only; the default is .\frigate\src\frigate.
'@ | Write-Host
}

try {
  if ($Command -eq 'help') { Show-Help; exit 0 }
  $devSourcePath = $null
  if ($Command -in @('dev-start','dev-restart','dev-logs','dev-stop')) {
    $devSourcePath = Resolve-DevSourcePath $SourceDir
    $env:CAMERA_SOURCE_OVERLAY = $devSourcePath
  }
  $effectiveCommand = switch ($Command) {
    'dev-start' { 'start' }
    'dev-logs' { 'logs' }
    'dev-stop' { 'stop' }
    default { $Command }
  }
  $config = Get-CameraConfig
  $runtime = Get-Runtime $config
  $notificationsEnabled = $null -ne $config.notifications -and [bool](Get-Value $config.notifications 'enabled' $false)
  if ($Command -eq 'build') { Build-RuntimeImage $runtime; exit 0 }
  $topology = Initialize-PlatformTopology
  $trackerNodes = @($topology.Nodes)
  $runtime.ExternalRecognition = [bool]$topology.Manifest.recognition.external
  $sources = @(Resolve-CameraSources $config $runtime)
  $hasReplay = @($sources | Where-Object Mode -eq 'replay').Count -gt 0
  Set-ComposeEnvironment $runtime
  New-ReplayOverride $sources $runtime $notificationsEnabled $trackerNodes
  $prefix = Get-ComposePrefix $hasReplay $runtime.ExternalRecognition ($trackerNodes.Count -gt 0)
  $edgeCameras = @($trackerNodes | ForEach-Object { $_.Cameras })
  $mainSources = @($sources | Where-Object { $edgeCameras -notcontains $_.Name })

  switch ($effectiveCommand) {
    'doctor' {
      Test-RuntimeDependencies $runtime
      Test-TrackerDependencies $runtime $trackerNodes
      $ngrokUrl = if ($notificationsEnabled) { Test-NgrokConfiguration $config } else { '' }
      $probes = @(Test-Sources $sources $runtime.Transport)
      Test-FrigateConfig $runtime $sources
      Invoke-Compose $prefix @('config','--quiet')
      if ((& docker ps --format '{{.Names}}') -contains 'camera-ngrok') {
        $actual = (Get-NgrokTunnelUrl).TrimEnd('/')
        if ($actual -ne $ngrokUrl) { Write-Warning 'Running ngrok tunnel is degraded or does not match NGROK_URL.' }
      }
      $state = [ordered]@{ checked_at=[DateTime]::UtcNow.ToString('o'); cameras=@() }
      foreach ($source in $sources) {
        $probe = $probes | Where-Object Name -eq $source.Name | Select-Object -First 1
        $state.cameras += [ordered]@{ name=$source.Name; mode=$source.Mode; source=$source.Redacted; codec=$probe.Codec; width=$probe.Width; height=$probe.Height }
      }
      Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 5) + "`n")
      Write-Host "Doctor passed for $($sources.Count) camera(s); no services started."
    }
    'start' {
      Test-RuntimeDependencies $runtime
      Test-TrackerDependencies $runtime $trackerNodes
      $ngrokUrl = if ($notificationsEnabled) { Test-NgrokConfiguration $config } else { '' }
      if (-not $notificationsEnabled -and ((& docker ps -a --format '{{.Names}}') -contains 'camera-ngrok')) {
        & docker rm -f camera-ngrok *> $null
      }
      $probes = @(Test-Sources $sources $runtime.Transport)
      Test-FrigateConfig $runtime $sources
      Ensure-FrigateConfigVolume
      Invoke-Compose $prefix @('config','--quiet')
      $replaySources = @($sources | Where-Object Mode -eq 'replay')
      if ($replaySources.Count -gt 0) {
        Invoke-Compose $prefix @('up','-d','--no-build','mediamtx')
        $replayServices = @($replaySources | ForEach-Object { 'replay-' + $_.Name.ToLowerInvariant().Replace('_','-') })
        Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + $replayServices)
        Wait-ReplayReady $replaySources
      }
      if ($runtime.ExternalRecognition) {
        Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','recognition')
        Wait-RecognitionReady
      } elseif ((& docker ps -a --format '{{.Names}}') -contains 'camera-recognition') {
        & docker rm -f camera-recognition *> $null
      }
      $trackerReadiness = @()
      if ($trackerNodes.Count -gt 0) {
        Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + @($trackerNodes.Service))
        $trackerReadiness = @(Wait-TrackerReady $trackerNodes)
      }
      if (-not $runtime.ExternalRecognition) {
        if ((& docker ps -a --format '{{.Names}}') -contains 'camera-recognition') {
          & docker rm -f camera-recognition *> $null
        }
      }
      Invoke-Compose $prefix @('up','-d','--no-build','--remove-orphans','--force-recreate','--no-deps','frigate')
      Invoke-Compose $prefix @('up','-d','--no-build')
      if ($env:CAMERA_SKIP_READY_WAIT -ne '1') {
        Wait-RuntimeReady $mainSources $config
        Wait-TrackerReady $trackerNodes
      }
      $ngrokReady = if ($notificationsEnabled) { Wait-NgrokReady $ngrokUrl } else { $false }
      $state = [ordered]@{
        started_at=[DateTime]::UtcNow.ToString('o')
        development=($null -ne $devSourcePath)
        source_overlay=$devSourcePath
        tracker_nodes=@($trackerNodes | ForEach-Object { [ordered]@{ node_id=$_.Id; cameras=$_.Cameras; container=$_.Container } })
        cameras=@()
      }
      foreach ($source in $sources) { $state.cameras += [ordered]@{ name=$source.Name; mode=$source.Mode; source=$source.Redacted } }
      Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 5) + "`n")
      Write-Host "Runtime ready with $($sources.Count) camera(s); public tunnel: $(if (-not $notificationsEnabled) { 'disabled' } elseif ($ngrokReady) { 'ready' } else { 'degraded' })."
      Show-Status
    }
    'dev-restart' {
      Test-RuntimeDependencies $runtime
      Ensure-FrigateConfigVolume
      Invoke-Compose $prefix @('config','--quiet')
      Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','frigate')
      if ($env:CAMERA_SKIP_READY_WAIT -ne '1') {
        Wait-RuntimeReady $sources $config
      }
      $state = [ordered]@{
        started_at=[DateTime]::UtcNow.ToString('o')
        development=$true
        source_overlay=$devSourcePath
        cameras=@()
      }
      foreach ($source in $sources) { $state.cameras += [ordered]@{ name=$source.Name; mode=$source.Mode; source=$source.Redacted } }
      Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 5) + "`n")
      Write-Host "Development Frigate restarted from source: $devSourcePath"
      Show-Status
    }
    'acceptance-start' {
      # The passage acceptance validator already performs strict fixture/config checks and
      # owns readiness/stability gates. Keep this switch bounded to compose
      # recreation so startup and rollback remain inside its 119-second budget.
      Ensure-FrigateConfigVolume
      Invoke-Compose $prefix @('config','--quiet')
      # SQLite must finish WAL/checkpoint work before Compose replaces the
      # container or an acceptance timeout can surface as a disk I/O failure.
      Invoke-Compose $prefix @('stop','--timeout','10','frigate')
      if ($runtime.ExternalRecognition) {
        Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','recognition')
        Wait-RecognitionReady
      } elseif ((& docker ps -a --format '{{.Names}}') -contains 'camera-recognition') {
        & docker rm -f camera-recognition *> $null
      }
      $acceptanceServices = [Collections.Generic.List[string]]::new()
      $replaySources = @($sources | Where-Object Mode -eq 'replay')
      if ($replaySources.Count -gt 0) {
        Invoke-Compose $prefix @('up','-d','--no-build','mediamtx')
        foreach ($source in $replaySources) {
          $acceptanceServices.Add(('replay-' + $source.Name.ToLowerInvariant().Replace('_','-')))
        }
        # Bring the RTSP paths online before Frigate opens them. Starting all
        # services in one Compose transaction races Frigate capture against
        # MediaMTX publication and can leave both cameras at 0 FPS for the
        # validator's entire startup budget.
        Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + @($acceptanceServices))
        Start-Sleep -Milliseconds 1200
        foreach ($source in $replaySources) {
          $container = 'camera-replay-' + $source.Name.ToLowerInvariant().Replace('_','-')
          $running = (& docker inspect $container --format '{{.State.Running}}' 2>$null).Trim()
          if ($running -ne 'true') { throw "Replay publisher '$($source.Name)' did not start." }
        }
        Wait-ReplayReady $replaySources
      }
      $trackerReadiness = @()
      if ($trackerNodes.Count -gt 0) {
        Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + @($trackerNodes.Service))
        $trackerReadiness = @(Wait-TrackerReady $trackerNodes)
      }
      Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','frigate')
      $sourceCommit = ((& git -C (Join-Path $workspace 'frigate') rev-parse HEAD) -join '').Trim()
      $worktreeHash = ((& git -C (Join-Path $workspace 'frigate') diff --binary HEAD | git hash-object --stdin) -join '').Trim()
      $imageManifest = if (Test-Path -LiteralPath $imageManifestFile) {
        Get-Content -LiteralPath $imageManifestFile -Encoding utf8 -Raw | ConvertFrom-Json
      } else { $null }
      $state = [ordered]@{
        started_at=[DateTime]::UtcNow.ToString('o')
        launcher='deploy/run.ps1 acceptance-start'
        config_hash=(Get-FileHash -Algorithm SHA256 -LiteralPath $configFile).Hash.ToLowerInvariant()
        topology_hash=[string]$topology.Manifest.topology_hash
        source_commit=$sourceCommit
        worktree_hash=$worktreeHash
        image=$imageManifest
        tracker_nodes=@($trackerReadiness)
        cameras=@()
      }
      foreach ($source in $sources) { $state.cameras += [ordered]@{ name=$source.Name; mode=$source.Mode; source=$source.Redacted } }
      Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 5) + "`n")
      Write-Host "Acceptance runtime containers recreated for $($sources.Count) camera(s)."
    }
    'acceptance-fault' {
      if ([string]::IsNullOrWhiteSpace($FaultScenario)) { throw 'acceptance-fault requires -FaultScenario.' }
      if ($FaultScenario -in @('tracker_restart','spool_replay','media_unavailable') -and $trackerNodes.Count -eq 0) {
        throw "acceptance-fault '$FaultScenario' requires a managed tracker node."
      }
      $startedAt = [DateTime]::UtcNow
      $faultState = [ordered]@{
        schema_version=1; scenario=$FaultScenario; started_at=$startedAt.ToString('o')
        launcher='deploy/run.ps1 acceptance-fault'; restored=$false
        tracker_nodes=@($trackerNodes | ForEach-Object { $_.Id })
      }
      try {
        switch ($FaultScenario) {
          'service_restart' {
            if (-not $runtime.ExternalRecognition) { throw 'service_restart requires external recognition.' }
            Invoke-Compose $prefix @('restart','recognition')
            Wait-RecognitionReady
          }
          'tracker_restart' {
            Invoke-Compose $prefix (@('restart') + @($trackerNodes.Service))
            $faultState.tracker_readiness = @(Wait-TrackerReady $trackerNodes)
          }
          'stream_disconnect' {
            $targets = if ($trackerNodes.Count -gt 0) { @($trackerNodes.Container) } else { @('frigate') }
            foreach ($target in $targets) {
              & docker exec $target sh -c "pkill -TERM -f '[f]fmpeg' || true" *> $null
              if ($LASTEXITCODE -ne 0) { throw "Unable to inject stream disconnect in $target." }
            }
            Start-Sleep -Seconds 3
            if ($trackerNodes.Count -gt 0) {
              $faultState.tracker_readiness = @(Wait-TrackerReady $trackerNodes)
            }
          }
          'client_disconnect' {
            Invoke-Compose $prefix @('stop','--timeout','5','frigate')
            Start-Sleep -Seconds 2
            Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','frigate')
          }
          'spool_replay' {
            Invoke-Compose $prefix @('stop','--timeout','5','frigate')
            Start-Sleep -Seconds 4
            $faultState.offline_seconds = 4
            Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','frigate')
            $faultState.tracker_readiness = @(Wait-TrackerReady $trackerNodes)
          }
          'media_unavailable' {
            Invoke-Compose $prefix (@('stop','--timeout','5') + @($trackerNodes.Service))
            Start-Sleep -Seconds 2
            Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + @($trackerNodes.Service))
            $faultState.tracker_readiness = @(Wait-TrackerReady $trackerNodes)
          }
        }
        $faultState.restored = $true
      } finally {
        $faultState.finished_at = [DateTime]::UtcNow.ToString('o')
        $faultState.duration_seconds = ([DateTime]::UtcNow - $startedAt).TotalSeconds
        Write-AtomicUtf8 (Join-Path $runtimeDir 'fault.json') (($faultState | ConvertTo-Json -Depth 8) + "`n")
      }
      Write-Host "Acceptance fault '$FaultScenario' completed and restored through deploy/run.ps1."
    }
    'acceptance-restore' {
      # Replay publishers are independent of the mounted Frigate config. Keep
      # them alive during rollback and replace only Frigate, which is the sole
      # service that must remount deploy/config.yaml.
      Ensure-FrigateConfigVolume
      Invoke-Compose $prefix @('config','--quiet')
      Invoke-Compose $prefix @('stop','--timeout','10','frigate')
      if ($trackerNodes.Count -gt 0) {
        Invoke-Compose $prefix (@('up','-d','--no-build','--force-recreate','--no-deps') + @($trackerNodes.Service))
        Wait-TrackerReady $trackerNodes
      }
      Invoke-Compose $prefix @('up','-d','--no-build','--force-recreate','--no-deps','frigate')
      if (-not $runtime.ExternalRecognition -and ((& docker ps -a --format '{{.Names}}') -contains 'camera-recognition')) {
        & docker rm -f camera-recognition *> $null
      }
      $state = [ordered]@{ started_at=[DateTime]::UtcNow.ToString('o'); cameras=@() }
      foreach ($source in $sources) { $state.cameras += [ordered]@{ name=$source.Name; mode=$source.Mode; source=$source.Redacted } }
      Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 5) + "`n")
      Write-Host "Acceptance runtime config restored for $($sources.Count) camera(s)."
    }
    'status' {
      if ($trackerNodes.Count -gt 0) {
        $trackerState = @(Wait-TrackerReady $trackerNodes 15)
        $state = if (Test-Path -LiteralPath $stateFile -PathType Leaf) {
          Get-Content -LiteralPath $stateFile -Encoding utf8 -Raw | ConvertFrom-Json
        } else {
          [pscustomobject]@{ checked_at=[DateTime]::UtcNow.ToString('o') }
        }
        $state | Add-Member -NotePropertyName tracker_nodes -NotePropertyValue $trackerState -Force
        $state | Add-Member -NotePropertyName checked_at -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
        Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 8) + "`n")
      }
      Show-Status
    }
    'logs' {
      $logArgs = if ($Command -eq 'dev-logs') { @('logs','--follow','--tail','200','frigate') } else { @('logs','--tail','200') }
      & docker @prefix @logArgs 2>&1 | ForEach-Object { Protect-Text ([string]$_) $sources }
      if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    'stop' {
      Invoke-Compose $prefix @('down','--remove-orphans')
      $remaining = @(& docker ps --format '{{.Names}}') | Where-Object { $_ -eq 'frigate' -or $_ -like 'camera-*' }
      if ($remaining) { throw "Shutdown incomplete: $($remaining -join ', ')" }
      Write-Host 'Camera runtime stopped cleanly.'
    }
  }
  exit 0
} catch {
  $knownSources = if ($null -ne (Get-Variable sources -ErrorAction SilentlyContinue)) { $sources } else { @() }
  $failure = @(
    $_.Exception.GetType().FullName
    $_.Exception.Message
    $_.InvocationInfo.PositionMessage
    $_.ScriptStackTrace
  ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
  Write-Host (Protect-Text ($failure -join "`n") $knownSources) -ForegroundColor Red
  exit 1
}
