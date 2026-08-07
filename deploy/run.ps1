[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('help', 'start', 'status', 'logs', 'doctor', 'stop', 'build')]
  [string]$Command = 'help'
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$configFile = Join-Path $PSScriptRoot 'config.yaml'
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
  if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3 with PyYAML is required to read config.yaml.'
  }
  $python = "import json,sys,yaml; value=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(json.dumps(value,ensure_ascii=False))"
  $json = & python -c $python $configFile 2>&1
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
    CpuLimit = $cpu
    ModelPath = Resolve-WorkspacePath ([string](Get-Value $runtime 'model_path' 'models/yolov9-t-320.onnx'))
    MediaDir = Resolve-WorkspacePath ([string](Get-Value $runtime 'media_dir' 'E:/Docker/Frigate/media'))
    Transport = $transport
    ReplayLoop = [bool](Get-Value $runtime.replay 'loop' $true)
    ReplaySources = Get-Value $runtime.replay 'sources' ([pscustomobject]@{})
  }
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
  $replayNames = @($Runtime.ReplaySources.PSObject.Properties.Name)
  foreach ($name in $replayNames) {
    if ($Config.cameras.PSObject.Properties.Name -notcontains $name) {
      throw "runtime.replay.sources.$name has no matching cameras.$name entry."
    }
  }
  $sources = @()
  foreach ($camera in $Config.cameras.PSObject.Properties) {
    $name = $camera.Name
    if ($name -notmatch '^[A-Za-z0-9_-]+$') { throw "Invalid camera name '$name'." }
    $stream = Get-FirstStream $streams $name
    if ($replayNames -contains $name) {
      $path = Resolve-WorkspacePath ([string]$Runtime.ReplaySources.$name)
      if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Replay file for $name does not exist: $path" }
      $expected = "rtsp://mediamtx:18554/$name"
      if ($stream -ne $expected) { throw "go2rtc.streams.$name must be '$expected' for a replay source." }
      $sources += [pscustomobject]@{ Name=$name; Mode='replay'; Raw=$path; Path=$path; Redacted=$path }
    } else {
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
  $env:FRIGATE_CPU_LIMIT = [string]$Runtime.CpuLimit
  $env:CAMERA_CONFIG_FILE = $configFile.Replace('\','/')
  $env:CAMERA_MODEL_PATH = $Runtime.ModelPath.Replace('\','/')
  $env:FRIGATE_MEDIA_DIR = $Runtime.MediaDir.Replace('\','/')
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

function New-ReplayOverride([object[]]$Sources, $Runtime) {
  $lines = [Collections.Generic.List[string]]::new()
  $lines.Add('services:')
  $replays = @($Sources | Where-Object Mode -eq 'replay')
  if ($replays.Count -eq 0) { $lines.Add('  {}') }
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
    # Replay transports prepared H.264 media without decoding or re-encoding it.
    $commandArgs = @('-hide_banner','-loglevel','warning','-re','-stream_loop',$loop,'-fflags','+genpts','-i','/runtime/source','-map','0:v:0','-an','-c:v','copy','-f','rtsp','-rtsp_transport','tcp',("rtsp://mediamtx:18554/$($source.Name)"))
    $command = $commandArgs | ConvertTo-Json -Compress
    $lines.Add("    command: $command")
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

function Get-ComposePrefix([bool]$Replay) {
  if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw "Missing required secrets file: $envFile" }
  $args = @('compose','-f',$composeFile,'-f',$composeOverride,'--env-file',$envFile)
  if ($Replay) { $args += @('--profile','replay') }
  return $args
}

function Invoke-Compose([string[]]$Prefix, [string[]]$Arguments) {
  & docker @Prefix @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed with exit code $LASTEXITCODE." }
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
  if (-not (Test-Path -LiteralPath $Runtime.ModelPath -PathType Leaf)) { throw "Missing model: $($Runtime.ModelPath)" }
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
  if ($LASTEXITCODE -ne 0) { throw 'Unable to create immutable runtime image tag.' }
  $manifest = [ordered]@{
    source_image = $Runtime.ConfiguredImage
    image = $immutableImage
    digest = $imageId
    built_at = [DateTime]::UtcNow.ToString('o')
  }
  Write-AtomicUtf8 $imageManifestFile (($manifest | ConvertTo-Json) + "`n")
  $stopwatch.Stop()
  Write-Host ("Built runtime image: {0} in {1:n1}s (overlay only; full dependency build disabled)." -f $immutableImage,$stopwatch.Elapsed.TotalSeconds)
}

function Test-FrigateConfig($Runtime, [object[]]$Sources) {
  $validator = "from frigate.config import FrigateConfig; f=open('/config/config.yml',encoding='utf-8'); FrigateConfig.parse(f); f.close(); print('Frigate config: OK')"
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
      -v "${configFile}:/config/config.yml:ro" `
      -v "$($Runtime.ModelPath):/models/yolov9-t-320.onnx:ro" `
      $Runtime.Image -c $validator 2>&1
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $oldPreference
  }
  if ($exitCode -ne 0) { throw (Protect-Text ($output -join "`n") $Sources) }
}

function Wait-RuntimeReady([object[]]$Sources) {
  $deadline = [DateTime]::UtcNow.AddSeconds(180)
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
      $faceReady = $null -ne $stats.embeddings.face_recognition
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
          } elseif (([DateTime]::UtcNow - $stableSince).TotalSeconds -ge 60) {
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
  throw 'Runtime did not remain ready without camera restarts for 60 seconds within the 180-second startup window.'
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
  .\deploy\run.ps1 status
  .\deploy\run.ps1 logs
  .\deploy\run.ps1 doctor
  .\deploy\run.ps1 stop
  .\deploy\run.ps1 build

All runtime and Frigate settings are defined in .\deploy\config.yaml.
'@ | Write-Host
}

try {
  if ($Command -eq 'help') { Show-Help; exit 0 }
  $config = Get-CameraConfig
  $runtime = Get-Runtime $config
  if ($Command -eq 'build') { Build-RuntimeImage $runtime; exit 0 }
  $sources = @(Resolve-CameraSources $config $runtime)
  $hasReplay = @($sources | Where-Object Mode -eq 'replay').Count -gt 0
  Set-ComposeEnvironment $runtime
  New-ReplayOverride $sources $runtime
  $prefix = Get-ComposePrefix $hasReplay

  switch ($Command) {
    'doctor' {
      Test-RuntimeDependencies $runtime
      $ngrokUrl = Test-NgrokConfiguration $config
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
      $ngrokUrl = Test-NgrokConfiguration $config
      $probes = @(Test-Sources $sources $runtime.Transport)
      Test-FrigateConfig $runtime $sources
      Ensure-FrigateConfigVolume
      Invoke-Compose $prefix @('config','--quiet')
      Invoke-Compose $prefix @('up','-d','--no-build','--remove-orphans','--force-recreate')
      Wait-RuntimeReady $sources
      $ngrokReady = Wait-NgrokReady $ngrokUrl
      $state = [ordered]@{ started_at=[DateTime]::UtcNow.ToString('o'); cameras=@() }
      foreach ($source in $sources) { $state.cameras += [ordered]@{ name=$source.Name; mode=$source.Mode; source=$source.Redacted } }
      Write-AtomicUtf8 $stateFile (($state | ConvertTo-Json -Depth 5) + "`n")
      Write-Host "Runtime ready with $($sources.Count) camera(s); public tunnel: $(if ($ngrokReady) { 'ready' } else { 'degraded' })."
      Show-Status
    }
    'status' { Show-Status }
    'logs' {
      & docker @prefix logs --tail 200 2>&1 | ForEach-Object { Protect-Text ([string]$_) $sources }
      if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    'stop' {
      Invoke-Compose $prefix @('down','--remove-orphans')
      $remaining = @(& docker ps --format '{{.Names}}') | Where-Object { $_ -eq 'frigate' -or $_ -like 'camera-*' }
      if ($remaining) { throw "Shutdown incomplete: $($remaining -join ', ')" }
      Write-Host 'Camera runtime stopped cleanly.'
    }
  }
} catch {
  $knownSources = if ($null -ne (Get-Variable sources -ErrorAction SilentlyContinue)) { $sources } else { @() }
  Write-Host (Protect-Text $_.Exception.Message $knownSources) -ForegroundColor Red
  exit 1
}
