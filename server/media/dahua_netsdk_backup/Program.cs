using System.Text.Json;
using Dahua.Api;
using Dahua.Api.Abstractions;
using Dahua.Api.Data;

return await BackupApplication.RunAsync(args);

internal static class BackupApplication
{
    public static async Task<int> RunAsync(string[] args)
    {
        try
        {
            var options = BackupOptions.Parse(args);
            do
            {
                await RunOnceAsync(options);
                if (options.IntervalSeconds == 0) break;
                await Task.Delay(TimeSpan.FromSeconds(options.IntervalSeconds));
            } while (true);
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new { status = "error", message = exception.Message }));
            return 1;
        }
    }

    private static async Task RunOnceAsync(BackupOptions options)
    {
        Directory.CreateDirectory(options.OutputDirectory);
        var sdk = new DahuaSDK();
        IDahuaApi? session = null;
        try
        {
            sdk.Initialize();
            session = sdk.Login(options.Host, options.Port, options.Username, options.Password);
            session.CommandTimeout = options.CommandTimeoutMilliseconds;

            var now = session.ConfigService.GetTime();
            var from = now.AddHours(-options.LookbackHours);
            var closedBefore = now.AddSeconds(-options.SettleSeconds);
            var recordings = options.Channels
                .SelectMany(channel => session.VideoService.FindFiles(from, now, channel)
                    .Select(file => new Recording(channel, file)))
                .Where(item => item.File.Date.AddSeconds(item.File.Duration) <= closedBefore)
                .OrderByDescending(item => item.File.Date)
                .ToList();

            // Same safety rule as Hik.Web: never download the last, potentially open file.
            var closed = recordings
                .GroupBy(item => item.Channel)
                .SelectMany(group => group.OrderBy(item => item.File.Date).SkipLast(1))
                .OrderByDescending(item => item.File.Date)
                .ToList();
            var selected = options.LatestOnly ? closed.Take(1) : closed;
            var downloaded = new List<object>();
            foreach (var recording in selected)
            {
                var target = BuildTargetPath(options.OutputDirectory, recording);
                if (File.Exists(target)) continue;
                var temporary = target + ".part.mp4";
                if (File.Exists(temporary)) File.Delete(temporary);
                var handle = session.VideoService.StartDownloadFile(recording.File, temporary);
                if (handle <= 0) throw new InvalidOperationException($"Dahua refused download: {recording.File.Name}");
                try
                {
                    await WaitForDownloadAsync(session.VideoService, handle, options.DownloadTimeoutSeconds);
                }
                finally
                {
                    session.VideoService.StopDownloadFile(handle);
                }
                if (!File.Exists(temporary) || new FileInfo(temporary).Length == 0)
                    throw new InvalidOperationException($"Downloaded file is empty: {recording.File.Name}");
                File.Move(temporary, target, false);
                downloaded.Add(new { channel = recording.Channel + 1, source = recording.File.Name, output = target, bytes = new FileInfo(target).Length });
            }
            Console.WriteLine(JsonSerializer.Serialize(new { status = "ok", xvrTime = now, found = recordings.Count, eligible = closed.Count, downloaded }));
        }
        finally
        {
            session?.Logout();
            sdk.Cleanup();
        }
    }

    private static async Task WaitForDownloadAsync(IVideoService video, long handle, int timeoutSeconds)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            await Task.Delay(1000);
            var progress = video.GetDownloadPosition(handle);
            if (!progress.success) return; // NetSDK reports completion by closing the transfer.
            if (progress.totalSize > 0 && progress.downloadSize >= progress.totalSize) return;
        }
        throw new TimeoutException($"Dahua download exceeded {timeoutSeconds} seconds");
    }

    private static string BuildTargetPath(string outputDirectory, Recording recording)
    {
        var source = Path.GetFileNameWithoutExtension(recording.File.Name);
        foreach (var invalid in Path.GetInvalidFileNameChars()) source = source.Replace(invalid, '_');
        // Dahua.Api asks NetSDK for an .mp4 destination, but this XVR returns
        // a native DHAV payload. Preserve the truthful container extension.
        var name = $"XVR_ch{recording.Channel + 1}_{recording.File.Date:yyyyMMdd_HHmmss}_{source}.dav";
        return Path.Combine(outputDirectory, name);
    }

    private sealed record Recording(int Channel, IRemoteFile File);
}

internal sealed record BackupOptions(
    string Host, int Port, string Username, string Password, int[] Channels,
    int LookbackHours, int SettleSeconds, string OutputDirectory, bool LatestOnly,
    int IntervalSeconds, int CommandTimeoutMilliseconds, int DownloadTimeoutSeconds)
{
    public static BackupOptions Parse(string[] args)
    {
        var values = args.Chunk(2).Where(x => x.Length == 2).ToDictionary(x => x[0], x => x[1]);
        string Get(string name, string fallback) => values.GetValueOrDefault(name, fallback);
        int GetInt(string name, int fallback) => int.Parse(Get(name, fallback.ToString()));
        var password = Get("--password", Environment.GetEnvironmentVariable("DAHUA_PASSWORD") ?? "");
        if (string.IsNullOrWhiteSpace(password)) throw new ArgumentException("Set DAHUA_PASSWORD or pass --password");
        var channels = Get("--channels", "0,1,2,3,4,5,6,7").Split(',').Select(int.Parse).Distinct().ToArray();
        if (channels.Any(x => x < 0)) throw new ArgumentException("Channels are zero-based and must be non-negative");
        var defaultOutput = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "uploads", "videos"));
        return new(
            Get("--host", "192.168.100.229"), GetInt("--port", 37777), Get("--username", "admin"), password,
            channels, GetInt("--lookback-hours", 24), GetInt("--settle-seconds", 30),
            Path.GetFullPath(Get("--output-dir", defaultOutput)), Get("--mode", "latest") == "latest",
            GetInt("--interval-seconds", 0), GetInt("--command-timeout-ms", 10000), GetInt("--download-timeout-seconds", 600));
    }
}
