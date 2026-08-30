import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { TooltipProvider } from '@/components/ui/tooltip'
import { CameraCard } from '@/components/camera-card'
import { DashboardHeader } from '@/components/dashboard-header'
import { EventPanel } from '@/components/event-panel'
import { fetchEvents, fetchMetrics } from '@/lib/api'
import type { CameraDetail, EventRecord, MetricsResponse, PlayerState } from '@/types'

const MAX_EVENTS = 50
const EVENT_POLL_MS = 1000
const METRICS_POLL_MS = 2000

function eventTimestamp(event: EventRecord) {
  const timestamp = Number(event.timestamp)
  return Number.isFinite(timestamp) ? timestamp : Number.MAX_SAFE_INTEGER
}

function App() {
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null)
  const [events, setEvents] = useState<EventRecord[]>([])
  const [playerStates, setPlayerStates] = useState<Record<string, PlayerState>>({})
  const [apiError, setApiError] = useState(false)
  const [eventsLoading, setEventsLoading] = useState(true)
  const [eventsError, setEventsError] = useState(false)
  const [focusedCameraId, setFocusedCameraId] = useState<string | null>(null)
  const metricsRequestInFlight = useRef(false)
  const eventsRequestInFlight = useRef(false)
  const liveCameras = useRef<Set<string>>(new Set())
  const runtimeReady = useRef(false)

  const refreshMetrics = useCallback(async () => {
    if (metricsRequestInFlight.current) return
    metricsRequestInFlight.current = true
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
    } finally {
      metricsRequestInFlight.current = false
    }
  }, [])

  const refreshEvents = useCallback(async () => {
    if (eventsRequestInFlight.current) return
    eventsRequestInFlight.current = true
    try {
      const data = await fetchEvents()
      setEventsLoading(false)
      setEventsError(false)
      const current = [...(data.events ?? [])]
        .sort((left, right) => eventTimestamp(right) - eventTimestamp(left))
        .filter((event) => liveCameras.current.size === 0 || liveCameras.current.has(String(event.camera)))
      setEvents(current.slice(0, MAX_EVENTS))
    } catch {
      // Keep existing events visible while the API is unavailable.
      setEventsLoading(false)
      setEventsError(true)
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
      if (previous && previous.live === state.live && previous.error === state.error && previous.connecting === state.connecting && previous.transport === state.transport && previous.videoLatencyMs === state.videoLatencyMs && previous.videoLatencySource === state.videoLatencySource && previous.message === state.message) return current
      return { ...current, [cameraId]: state }
    })
  }, [])

  const handleCameraFocus = useCallback((cameraId: string) => {
    setFocusedCameraId(cameraId)
  }, [])

  const cameras: CameraDetail[] = metrics?.pipeline.camera_details ?? []
  const liveCount = cameras.filter((camera) => playerStates[camera.id]?.live).length
  const glassLatencyValues = cameras.map((camera) => playerStates[camera.id]?.videoLatencyMs).filter((value): value is number => value != null)
  const glassLatency = liveCount === 0
    ? (runtimeReady.current ? 'connecting' : 'offline')
    : glassLatencyValues.length ? `${Math.max(...glassLatencyValues).toFixed(0)} ms` : 'đang đo'
  const status = liveCount > 0
    ? liveCount === cameras.length ? 'LIVE' : `LIVE (${liveCount}/${cameras.length})`
    : apiError || !metrics?.pipeline.running ? 'Runtime offline' : 'Connecting...'
  const cameraKey = useMemo(() => cameras.map((camera) => camera.id).join('|'), [cameras])

  useEffect(() => {
    const ids = new Set(cameraKey ? cameraKey.split('|') : [])
    setPlayerStates((current) => Object.fromEntries(Object.entries(current).filter(([id]) => ids.has(id))))
    setFocusedCameraId((current) => current && ids.has(current) ? current : ids.values().next().value ?? null)
  }, [cameraKey])

  const hasFocusedCamera = focusedCameraId !== null
  const focusedLayoutClass = hasFocusedCamera
    ? cameras.length === 1
      ? 'camera-wall-focused-single'
      : cameras.length === 2
        ? 'camera-wall-focused-dual'
        : 'camera-wall-focused-multi'
    : ''

  return (
    <TooltipProvider>
      <main className="dashboard-shell mx-auto flex min-h-screen w-full max-w-[1600px] flex-col gap-2 px-3 py-4 sm:px-4 md:h-[100dvh] md:overflow-hidden lg:px-6">
        <DashboardHeader metrics={metrics} status={status} apiError={apiError} glassLatency={glassLatency} />
        <div className="dashboard-content grid min-h-0 flex-1 gap-4 md:overflow-hidden lg:grid-cols-[minmax(0,1fr)_minmax(320px,380px)]">
          <section className="flex min-h-0 min-w-0 flex-col gap-4 md:overflow-hidden">
            <div className={`camera-wall grid min-h-0 flex-1 grid-cols-1 content-start gap-3 overflow-y-auto overscroll-contain pr-1 md:grid-cols-2 ${hasFocusedCamera ? `camera-wall-focused ${focusedLayoutClass}` : ''}`}>
              {cameras.map((camera) => <CameraCard key={camera.id} camera={camera} focused={focusedCameraId === camera.id} onFocus={handleCameraFocus} onStateChange={handlePlayerState} />)}
              {cameras.length === 0 && <div className="camera-wall-empty"><span>{apiError ? 'Không thể đọc trạng thái camera' : 'Chưa có camera cấu hình'}</span><small>{apiError ? 'Dữ liệu cuối cùng sẽ được giữ lại khi kết nối lại.' : 'Runtime chưa cung cấp camera detail.'}</small></div>}
            </div>
          </section>
          <aside className="event-rail flex min-h-[420px] min-w-0 lg:min-h-0">
            <EventPanel events={events} loading={eventsLoading} error={eventsError} />
          </aside>
        </div>
      </main>
    </TooltipProvider>
  )
}

export default App
