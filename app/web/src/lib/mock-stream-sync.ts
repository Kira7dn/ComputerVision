interface SynchronizedVideo {
  video: HTMLVideoElement
  periodSeconds: number
  epochSeconds: number
}

interface LiveMetadataClock {
  timestamp?: number
}

const groups = new Map<string, Map<string, SynchronizedVideo>>()
let synchronizationTimer: number | null = null
let clockTimer: number | null = null
let serverClockOffsetSeconds: number | null = null
let clockRequestInFlight = false

function browserEpochSeconds() {
  return (performance.timeOrigin + performance.now()) / 1000
}

function circularDrift(current: number, target: number, duration: number) {
  const raw = current - target
  return ((raw + duration / 2) % duration + duration) % duration - duration / 2
}

async function refreshServerClock() {
  if (clockRequestInFlight) return
  clockRequestInFlight = true
  const requestStart = browserEpochSeconds()
  try {
    const response = await fetch('/api/live-metadata', { cache: 'no-store' })
    if (!response.ok) return
    const payload = await response.json() as LiveMetadataClock
    const serverTimestamp = Number(payload.timestamp)
    const requestEnd = browserEpochSeconds()
    if (!Number.isFinite(serverTimestamp)) return
    const offset = serverTimestamp - (requestStart + requestEnd) / 2
    serverClockOffsetSeconds = serverClockOffsetSeconds == null
      ? offset
      : serverClockOffsetSeconds * 0.8 + offset * 0.2
  } catch {
    // Keep the most recent clock mapping through transient API failures.
  } finally {
    clockRequestInFlight = false
  }
}

function synchronize() {
  if (serverClockOffsetSeconds == null) return
  const serverNow = browserEpochSeconds() + serverClockOffsetSeconds
  for (const streams of groups.values()) {
    const entries = [...streams.values()]
    if (entries.length === 0) continue
    const { periodSeconds, epochSeconds } = entries[0]
    const liveLatencies = entries
      .map(({ video }) => Number(video.dataset.syncLiveLatencySeconds))
      .filter((latency) => Number.isFinite(latency) && latency >= 0 && latency <= 30)
    const referenceLatency = liveLatencies.length > 0 ? Math.max(...liveLatencies) : 0
    const timelineTimestamp = serverNow - referenceLatency
    const phase = ((timelineTimestamp - epochSeconds) % periodSeconds + periodSeconds) % periodSeconds
    const normalizedPhase = phase / periodSeconds

    for (const { video } of entries) {
      video.dataset.syncPhase = normalizedPhase.toFixed(9)
      video.dataset.syncTimelineTimestamp = timelineTimestamp.toFixed(6)
      if (
        video.readyState < HTMLMediaElement.HAVE_METADATA
        || !Number.isFinite(video.duration)
        || video.duration <= 0
      ) continue
      const target = normalizedPhase * video.duration
      const drift = circularDrift(video.currentTime, target, video.duration)
      video.dataset.syncTargetSeconds = target.toFixed(6)
      video.dataset.syncDriftSeconds = drift.toFixed(6)
      if (video.seeking) {
        video.dataset.syncSeeking = 'true'
        continue
      }
      delete video.dataset.syncSeeking
      if (Math.abs(drift) > 0.25) {
        video.currentTime = target
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

function ensureTimers() {
  if (synchronizationTimer === null) {
    synchronizationTimer = window.setInterval(synchronize, 250)
  }
  if (clockTimer === null) {
    void refreshServerClock()
    clockTimer = window.setInterval(() => void refreshServerClock(), 2_000)
  }
}

function stopTimersWhenIdle() {
  if ([...groups.values()].some((streams) => streams.size > 0)) return
  if (synchronizationTimer !== null) window.clearInterval(synchronizationTimer)
  if (clockTimer !== null) window.clearInterval(clockTimer)
  synchronizationTimer = null
  clockTimer = null
  serverClockOffsetSeconds = null
}

export function registerSynchronizedMock(
  group: string,
  cameraId: string,
  video: HTMLVideoElement,
  periodSeconds: number,
  epochSeconds: number,
) {
  if (!Number.isFinite(periodSeconds) || periodSeconds <= 0 || !Number.isFinite(epochSeconds)) {
    throw new Error(`Invalid synchronized mock timeline for ${cameraId}`)
  }
  const streams = groups.get(group) ?? new Map<string, SynchronizedVideo>()
  const existing = streams.values().next().value as SynchronizedVideo | undefined
  if (
    existing
    && (existing.periodSeconds !== periodSeconds || existing.epochSeconds !== epochSeconds)
  ) {
    throw new Error(`Camera ${cameraId} differs from synchronized mock group ${group}`)
  }
  video.dataset.cameraId = cameraId
  video.dataset.syncGroup = group
  video.dataset.syncPeriodSeconds = String(periodSeconds)
  video.dataset.syncEpochSeconds = String(epochSeconds)
  streams.set(cameraId, { video, periodSeconds, epochSeconds })
  groups.set(group, streams)
  ensureTimers()
  return () => {
    video.playbackRate = 1
    delete video.dataset.syncPhase
    delete video.dataset.syncTimelineTimestamp
    delete video.dataset.syncTargetSeconds
    delete video.dataset.syncDriftSeconds
    delete video.dataset.syncSeeking
    const current = groups.get(group)
    current?.delete(cameraId)
    if (current?.size === 0) groups.delete(group)
    stopTimersWhenIdle()
  }
}
