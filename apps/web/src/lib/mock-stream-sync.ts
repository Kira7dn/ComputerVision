import { subscribeLiveMetadata, type LiveMetadataSnapshot } from '@/lib/live-metadata'

interface SynchronizedVideo {
  video: HTMLVideoElement
  periodSeconds: number
  epochSeconds: number
}

const groups = new Map<string, Map<string, SynchronizedVideo>>()
let synchronizationTimer: number | null = null
let clockCleanup: (() => void) | null = null
let serverClockOffsetSeconds: number | null = null

function browserEpochSeconds() {
  return (performance.timeOrigin + performance.now()) / 1000
}

function circularDrift(current: number, target: number, duration: number) {
  const raw = current - target
  return ((raw + duration / 2) % duration + duration) % duration - duration / 2
}

function refreshServerClock(snapshot: LiveMetadataSnapshot) {
  const serverTimestamp = Number(snapshot.payload.timestamp)
  if (!Number.isFinite(serverTimestamp)) return
  const browserMidpointSeconds = snapshot.browserMidpointEpochMs / 1000
  const offset = serverTimestamp - browserMidpointSeconds
  serverClockOffsetSeconds = serverClockOffsetSeconds == null
    ? offset
    : serverClockOffsetSeconds * 0.8 + offset * 0.2
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
  if (clockCleanup === null) {
    clockCleanup = subscribeLiveMetadata(refreshServerClock, 2_000)
  }
}

function stopTimersWhenIdle() {
  if ([...groups.values()].some((streams) => streams.size > 0)) return
  if (synchronizationTimer !== null) window.clearInterval(synchronizationTimer)
  clockCleanup?.()
  synchronizationTimer = null
  clockCleanup = null
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
