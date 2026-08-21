import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { TooltipProvider } from '@/components/ui/tooltip'
import { CameraCard } from '@/components/camera-card'
import { EventPanel } from '@/components/event-panel'
import { MetricsGrid } from '@/components/metrics-grid'
import { fetchEvents, fetchMetrics } from '@/lib/api'
import type { CameraDetail, EventRecord, MetricsResponse, PlayerState } from '@/types'

const MAX_EVENTS = 10
const EVENT_POLL_MS = 1000
const METRICS_POLL_MS = 2000

function eventId(event: EventRecord) {
  return String(event.event_id ?? `${event.camera}-${event.timestamp}-${event.event_name}`)
}

function eventTimestamp(event: EventRecord) {
  const timestamp = Number(event.timestamp)
  return Number.isFinite(timestamp) ? timestamp : Number.MAX_SAFE_INTEGER
}

function mergeEvents(current: EventRecord[], incoming: EventRecord[]) {
  const entries = new Map(current.map((event) => [eventId(event), event]))
  for (const event of incoming) entries.set(eventId(event), event)
  return [...entries.values()].sort((left, right) => eventTimestamp(right) - eventTimestamp(left)).slice(0, MAX_EVENTS)
}

function App() {
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null)
  const [events, setEvents] = useState<EventRecord[]>([])
  const [playerStates, setPlayerStates] = useState<Record<string, PlayerState>>({})
  const [apiError, setApiError] = useState(false)
  const eventCursor = useRef<number | null>(null)
  const eventRunId = useRef<string | null>(null)
  const eventsRequestInFlight = useRef(false)
  const liveCameras = useRef<Set<string>>(new Set())
  const runtimeReady = useRef(false)

  const refreshMetrics = useCallback(async () => {
    try {
      const data = await fetchMetrics()
      const cameras = data.pipeline.camera_details ?? []
      const live = new Set(cameras.filter((camera) => camera.running && Boolean(camera.worker_ready ?? camera.ready)).map((camera) => camera.id))
      liveCameras.current = live
      runtimeReady.current = Boolean(data.pipeline.running && cameras.length > 0 && live.size === cameras.length)
      setMetrics(data)
      setApiError(false)
    } catch {
      setApiError(true)
      // Preserve the last good metrics, camera state and event history during transient API failures.
    }
  }, [])

  const refreshEvents = useCallback(async () => {
    if (eventsRequestInFlight.current) return
    eventsRequestInFlight.current = true
    try {
      const cursor = eventCursor.current
      const data = await fetchEvents(cursor ?? 0, cursor === null ? MAX_EVENTS * 2 : undefined)
      if (cursor === null) {
        eventRunId.current = data.run_id
        eventCursor.current = data.cursor
        const initial = [...(data.events ?? [])]
          .sort((left, right) => Number(left.sequence ?? 0) - Number(right.sequence ?? 0))
          .filter((event) => liveCameras.current.size === 0 || liveCameras.current.has(String(event.camera)))
        setEvents((current) => mergeEvents(current, initial))
        return
      }
      if (data.run_id !== eventRunId.current) {
        eventRunId.current = data.run_id
        eventCursor.current = data.cursor
        setEvents([])
        return
      }
      eventCursor.current = data.cursor
      if (runtimeReady.current) {
        const fresh = [...(data.events ?? [])]
          .sort((left, right) => Number(left.sequence ?? 0) - Number(right.sequence ?? 0))
          .filter((event) => liveCameras.current.has(String(event.camera)))
        if (fresh.length) setEvents((current) => mergeEvents(current, fresh))
      }
    } catch {
      // Keep existing events visible while the API is unavailable.
    } finally {
      eventsRequestInFlight.current = false
    }
  }, [])

  useEffect(() => {
    void refreshMetrics()
    const timer = window.setInterval(() => void refreshMetrics(), METRICS_POLL_MS)
    return () => window.clearInterval(timer)
  }, [refreshMetrics])

  useEffect(() => {
    void refreshEvents()
    const timer = window.setInterval(() => void refreshEvents(), EVENT_POLL_MS)
    return () => window.clearInterval(timer)
  }, [refreshEvents])

  const handlePlayerState = useCallback((cameraId: string, state: PlayerState) => {
    setPlayerStates((current) => {
      const previous = current[cameraId]
      if (previous && previous.live === state.live && previous.error === state.error && previous.connecting === state.connecting && previous.transport === state.transport && previous.jitterBufferDelayMs === state.jitterBufferDelayMs && previous.message === state.message) return current
      return { ...current, [cameraId]: state }
    })
  }, [])

  const cameras: CameraDetail[] = metrics?.pipeline.camera_details ?? []
  const liveCount = cameras.filter((camera) => playerStates[camera.id]?.live).length
  const fallbackCount = cameras.filter((camera) => playerStates[camera.id]?.live && playerStates[camera.id]?.transport === 'hls-fallback').length
  const jitterValues = cameras.map((camera) => playerStates[camera.id]?.jitterBufferDelayMs).filter((value): value is number => value != null)
  const browserLatency = liveCount === 0 ? (runtimeReady.current ? 'connecting' : 'offline') : jitterValues.length ? `jitter ${Math.max(...jitterValues).toFixed(0)} ms` : 'connected'
  const status = liveCount > 0
    ? liveCount === cameras.length ? 'LIVE' : `LIVE (${liveCount}/${cameras.length})`
    : apiError || !metrics?.pipeline.running ? 'Runtime offline' : 'Connecting...'
  const streamText = liveCount > 0
    ? fallbackCount ? `WebRTC: ${liveCount - fallbackCount}/${cameras.length}; HLS fallback: ${fallbackCount}` : `WebRTC: ${liveCount}/${cameras.length} camera streams`
    : 'WebRTC: offline'
  const ready = Boolean(metrics?.pipeline.ready && cameras.length > 0)
  const statusVariant = status === 'LIVE' ? 'default' : status.startsWith('LIVE') ? 'secondary' : 'outline'
  const cameraKey = useMemo(() => cameras.map((camera) => camera.id).join('|'), [cameras])

  useEffect(() => {
    const ids = new Set(cameraKey ? cameraKey.split('|') : [])
    setPlayerStates((current) => Object.fromEntries(Object.entries(current).filter(([id]) => ids.has(id))))
  }, [cameraKey])

  return (
    <TooltipProvider>
      <main className="mx-auto flex min-h-screen w-full max-w-[1440px] flex-col gap-4 px-4 py-5 lg:px-6">
        <header className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Camera live dashboard</h1>
            <p className="text-sm text-muted-foreground">DeepStream outputs</p>
          </div>
          <Badge variant={statusVariant}>{status}</Badge>
        </header>
        <Separator />
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,380px)]">
          <section className="flex min-h-0 min-w-0 flex-col gap-4">
            <div className="grid min-h-0 flex-1 grid-cols-1 content-start gap-3 md:grid-cols-2">
              {cameras.map((camera) => <CameraCard key={camera.id} camera={camera} onStateChange={handlePlayerState} />)}
              {cameras.length === 0 && <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">{ready ? 'No cameras configured' : 'Runtime offline'}</div>}
            </div>
            <MetricsGrid metrics={metrics} browserLatency={browserLatency} />
            <footer className="flex justify-between gap-4 text-xs text-muted-foreground">
              <span>DeepStream outputs</span>
              <span>{streamText}</span>
            </footer>
          </section>
          <aside className="flex min-h-[420px] min-w-0 lg:min-h-0">
            <EventPanel events={events} />
          </aside>
        </div>
      </main>
    </TooltipProvider>
  )
}

export default App
