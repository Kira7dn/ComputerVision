const groups = new Map<string, Map<string, HTMLVideoElement>>()
let timer: number | null = null

function synchronize() {
  for (const streams of groups.values()) {
    const videos = [...streams.values()].filter(
      (video) => video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && Number.isFinite(video.duration) && video.duration > 0,
    )
    if (videos.length < 2) continue
    const master = videos[0]
    const period = master.duration
    for (const video of videos.slice(1)) {
      const rawDrift = video.currentTime - master.currentTime
      const drift = ((rawDrift + period / 2) % period + period) % period - period / 2
      if (Math.abs(drift) > 0.25) {
        video.currentTime = master.currentTime
        video.playbackRate = 1
      } else if (drift > 0.02) {
        video.playbackRate = 0.98
      } else if (drift < -0.02) {
        video.playbackRate = 1.02
      } else {
        video.playbackRate = 1
      }
    }
  }
}

function ensureTimer() {
  if (timer === null) timer = window.setInterval(synchronize, 250)
}

function stopTimerWhenIdle() {
  if ([...groups.values()].some((streams) => streams.size > 0) || timer === null) return
  window.clearInterval(timer)
  timer = null
}

export function registerSynchronizedMock(group: string, cameraId: string, video: HTMLVideoElement) {
  const streams = groups.get(group) ?? new Map<string, HTMLVideoElement>()
  streams.set(cameraId, video)
  groups.set(group, streams)
  ensureTimer()
  return () => {
    video.playbackRate = 1
    const current = groups.get(group)
    current?.delete(cameraId)
    if (current?.size === 0) groups.delete(group)
    stopTimerWhenIdle()
  }
}
