import { subscribeLiveMetadata, type LiveMetadataSnapshot } from '@/lib/live-metadata'

interface SynchronizedVideo {
  video: HTMLVideoElement
  periodSeconds: number
  epochSeconds: number
  mediaOnly: boolean
}

export interface MockSyncGroupHealth {
  group: string
  locked: boolean
  serverLocked: boolean
  expectedMembers: number
  registeredMembers: number
  playingMembers: number
  referenceLatencySeconds: number | null
  p95DriftMs: number | null
  maxDriftMs: number | null
  seekCount: number
  reconnectCount: number
  stableSince: number | null
  updatedAt: number
}

interface DriftSample {
  timestamp: number
  driftMs: number
}

const groups = new Map<string, Map<string, SynchronizedVideo>>()
let synchronizationTimer: number | null = null
let clockCleanup: (() => void) | null = null
let serverClockOffsetSeconds: number | null = null
let latestTimeline: LiveMetadataSnapshot['payload']['mock_timeline'] | undefined
const groupHealth = new Map<string, MockSyncGroupHealth>()
const driftSamples = new Map<string, DriftSample[]>()
const STABLE_LOCK_SECONDS = 2
const DRIFT_WINDOW_SECONDS = 30
const MAX_DRIFT_MS = 250

function browserEpochSeconds() {
  return (performance.timeOrigin + performance.now()) / 1000
}

function circularDrift(current: number, target: number, duration: number) {
  const raw = current - target
  return ((raw + duration / 2) % duration + duration) % duration - duration / 2
}

function refreshServerClock(snapshot: LiveMetadataSnapshot) {
  latestTimeline = snapshot.payload.mock_timeline
  const serverTimestamp = Number(snapshot.payload.timestamp)
  if (!Number.isFinite(serverTimestamp)) return
  const browserMidpointSeconds = snapshot.browserMidpointEpochMs / 1000
  const offset = serverTimestamp - browserMidpointSeconds
  serverClockOffsetSeconds = serverClockOffsetSeconds == null
    ? offset
    : serverClockOffsetSeconds * 0.8 + offset * 0.2
}

function percentile95(values: number[]) {
  if (values.length === 0) return null
  const ordered = [...values].sort((left, right) => left - right)
  return ordered[Math.min(ordered.length - 1, Math.ceil(ordered.length * 0.95) - 1)]
}

function publishGroupHealth(group: string, health: MockSyncGroupHealth, entries: SynchronizedVideo[]) {
  groupHealth.set(group, health)
  for (const { video } of entries) {
    video.dataset.syncLocked = String(health.locked)
    video.dataset.syncExpectedMembers = String(health.expectedMembers)
    video.dataset.syncRegisteredMembers = String(health.registeredMembers)
    video.dataset.syncPlayingMembers = String(health.playingMembers)
    video.dataset.syncP95DriftMs = health.p95DriftMs == null ? '' : health.p95DriftMs.toFixed(3)
    video.dataset.syncMaxDriftMs = health.maxDriftMs == null ? '' : health.maxDriftMs.toFixed(3)
    video.dataset.syncReferenceLatencySeconds = health.referenceLatencySeconds == null
      ? ''
      : health.referenceLatencySeconds.toFixed(6)
  }
}

function synchronize() {
  if (serverClockOffsetSeconds == null) return
  const serverNow = browserEpochSeconds() + serverClockOffsetSeconds
  for (const [groupName, streams] of groups) {
    const entries = [...streams.values()]
    if (entries.length === 0) continue
    const { periodSeconds, epochSeconds } = entries[0]
    const liveLatencies = entries
      .filter(({ mediaOnly }) => !mediaOnly)
      .map(({ video }) => Number(video.dataset.syncLiveLatencySeconds))
      .filter((latency) => Number.isFinite(latency) && latency >= 0 && latency <= 30)
    const referenceLatency = liveLatencies.length > 0 ? Math.max(...liveLatencies) : 0
    const timelineTimestamp = serverNow - referenceLatency
    const phase = ((timelineTimestamp - epochSeconds) % periodSeconds + periodSeconds) % periodSeconds
    const normalizedPhase = phase / periodSeconds

    const directDrifts: number[] = []
    let correctionSeek = false
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
      directDrifts.push(Math.abs(drift) * 1000)
      video.dataset.syncTargetSeconds = target.toFixed(6)
      video.dataset.syncDriftSeconds = drift.toFixed(6)
      if (video.seeking) {
        video.dataset.syncSeeking = 'true'
        continue
      }
      delete video.dataset.syncSeeking
      if (Math.abs(drift) > 0.25) {
        correctionSeek = true
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
    const now = browserEpochSeconds()
    const serverGroup = latestTimeline?.groups?.[groupName]
    const expectedMembers = Object.keys(serverGroup?.cameras ?? {}).length
    const playingMembers = entries.filter(({ video }) => (
      video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && !video.paused
    )).length
    const hasLiveMember = entries.some(({ mediaOnly }) => !mediaOnly)
    const liveReferenceReady = !hasLiveMember || liveLatencies.length > 0
    const serverLocked = Boolean(latestTimeline?.ready && serverGroup?.locked)
    const previous = groupHealth.get(groupName)
    const healthy = serverLocked
      && expectedMembers > 0
      && entries.length === expectedMembers
      && playingMembers === expectedMembers
      && liveReferenceReady
      && directDrifts.length > 0
      && directDrifts.every((drift) => drift <= MAX_DRIFT_MS)
      && !correctionSeek
      && entries.every(({ video }) => !video.seeking)
    const stableSince = healthy ? previous?.stableSince ?? now : null
    const samples = healthy
      ? [...(driftSamples.get(groupName) ?? []), ...directDrifts.map((driftMs) => ({ timestamp: now, driftMs }))]
          .filter((sample) => now - sample.timestamp <= DRIFT_WINDOW_SECONDS)
      : []
    driftSamples.set(groupName, samples)
    const values = samples.map((sample) => sample.driftMs)
    publishGroupHealth(groupName, {
      group: groupName,
      locked: Boolean(stableSince != null && now - stableSince >= STABLE_LOCK_SECONDS),
      serverLocked,
      expectedMembers,
      registeredMembers: entries.length,
      playingMembers,
      referenceLatencySeconds: liveReferenceReady ? referenceLatency : null,
      p95DriftMs: percentile95(values),
      maxDriftMs: values.length > 0 ? Math.max(...values) : null,
      seekCount: (previous?.seekCount ?? 0) + Number(correctionSeek),
      reconnectCount: previous?.reconnectCount ?? 0,
      stableSince,
      updatedAt: now,
    }, entries)
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
  latestTimeline = undefined
}

export function registerSynchronizedMock(
  group: string,
  cameraId: string,
  video: HTMLVideoElement,
  periodSeconds: number,
  epochSeconds: number,
  mediaOnly: boolean,
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
  const replacingExistingCamera = streams.has(cameraId)
  const priorHealth = groupHealth.get(group)
  streams.set(cameraId, { video, periodSeconds, epochSeconds, mediaOnly })
  groups.set(group, streams)
  if (priorHealth && replacingExistingCamera) {
    groupHealth.set(group, { ...priorHealth, locked: false, reconnectCount: priorHealth.reconnectCount + 1 })
  }
  ensureTimers()
  return () => {
    video.playbackRate = 1
    delete video.dataset.syncPhase
    delete video.dataset.syncTimelineTimestamp
    delete video.dataset.syncTargetSeconds
    delete video.dataset.syncDriftSeconds
    delete video.dataset.syncSeeking
    delete video.dataset.syncLocked
    delete video.dataset.syncExpectedMembers
    delete video.dataset.syncRegisteredMembers
    delete video.dataset.syncPlayingMembers
    delete video.dataset.syncP95DriftMs
    delete video.dataset.syncMaxDriftMs
    delete video.dataset.syncReferenceLatencySeconds
    const current = groups.get(group)
    current?.delete(cameraId)
    if (current?.size === 0) groups.delete(group)
    if (current?.size === 0) {
      groupHealth.delete(group)
      driftSamples.delete(group)
    }
    stopTimersWhenIdle()
  }
}

export function synchronizedMockStatus(): Record<string, MockSyncGroupHealth> {
  return Object.fromEntries(
    [...groupHealth.entries()].map(([group, health]) => [group, { ...health }]),
  )
}

declare global {
  interface Window {
    __LS_VISION_SYNC_STATUS__?: () => Record<string, MockSyncGroupHealth>
  }
}

window.__LS_VISION_SYNC_STATUS__ = synchronizedMockStatus
