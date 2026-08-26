export interface FrameTimingSample {
  rtp_timestamp: number
  capture_timestamp: number
  output_timestamp: number
}

export interface LiveCameraMetadata {
    width?: number
    height?: number
    frame_num?: number
    timestamp?: number
    frame_timing_samples?: FrameTimingSample[]
    front_assistance?: {
      overlay?: {
        segments?: Array<{
          x1: number
          y1: number
          x2: number
          y2: number
          color: [number, number, number, number]
          width: number
        }>
      }
    }
}

export interface LiveMetadataResponse {
  timestamp?: number
  cameras?: Record<string, LiveCameraMetadata>
  mock_timeline?: {
    ready?: boolean
    groups?: Record<string, {
      locked?: boolean
      cameras?: Record<string, { ready?: boolean }>
    }>
  }
}

export interface LiveMetadataSnapshot {
  payload: LiveMetadataResponse
  browserMidpointEpochMs: number
}

type Listener = (snapshot: LiveMetadataSnapshot) => void

const subscriptions = new Map<Listener, number>()
let timer: number | null = null
let requestInFlight = false
let latest: LiveMetadataSnapshot | null = null

async function refresh() {
  if (requestInFlight || subscriptions.size === 0) return
  requestInFlight = true
  const requestStart = performance.now()
  try {
    const response = await fetch('/api/live-metadata', { cache: 'no-store' })
    if (!response.ok) return
    const payload = await response.json() as LiveMetadataResponse
    const requestEnd = performance.now()
    latest = {
      payload,
      browserMidpointEpochMs: performance.timeOrigin + (requestStart + requestEnd) / 2,
    }
    for (const listener of subscriptions.keys()) listener(latest)
  } catch {
    // Subscribers keep their latest valid mapping during transient failures.
  } finally {
    requestInFlight = false
  }
}

function restartTimer() {
  if (timer !== null) window.clearInterval(timer)
  timer = null
  if (subscriptions.size === 0) return
  const intervalMs = Math.min(...subscriptions.values())
  timer = window.setInterval(() => void refresh(), intervalMs)
  void refresh()
}

export function subscribeLiveMetadata(
  listener: Listener,
  intervalMs = 250,
) {
  subscriptions.set(listener, Math.max(100, intervalMs))
  if (latest !== null) listener(latest)
  restartTimer()
  return () => {
    subscriptions.delete(listener)
    restartTimer()
  }
}
