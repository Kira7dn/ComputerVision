import { useEffect, useRef, useState } from 'react'
import type { CameraDetail, HlsConstructor, HlsInstance, MediaMTXReader, PlayerState } from '@/types'

const initialState: PlayerState = {
  live: false,
  error: false,
  connecting: true,
  transport: 'webrtc',
  jitterBufferDelayMs: null,
  message: 'Connecting...',
}

function loadHls(): Promise<HlsConstructor> {
  if (window.Hls) return Promise.resolve(window.Hls)
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.17'
    script.onload = () => window.Hls ? resolve(window.Hls) : reject(new Error('hls.js unavailable'))
    script.onerror = () => reject(new Error('hls.js load failed'))
    document.head.appendChild(script)
  })
}

export function useMediaStream(camera: CameraDetail, onStateChange: (state: PlayerState) => void) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const readerRef = useRef<MediaMTXReader | null>(null)
  const [state, setState] = useState<PlayerState>(initialState)

  useEffect(() => {
    onStateChange(state)
  }, [onStateChange, state])

  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    if (!camera.running) {
      setState({ ...initialState, connecting: false, error: true, message: 'Worker offline' })
      return
    }

    let reader: MediaMTXReader | null = null
    let hls: HlsInstance | null = null
    let fallbackCleanup: (() => void) | null = null
    let fallbackTimer: number | null = null
    let statsTimer: number | null = null
    let destroyed = false

    const update = (next: Partial<PlayerState>) => {
      if (!destroyed) setState((current) => ({ ...current, ...next }))
    }

    const startFallback = async () => {
      try {
        const Hls = await loadHls()
        if (destroyed || !Hls.isSupported()) throw new Error('HLS không được hỗ trợ')
        hls = new Hls({
          lowLatencyMode: true,
          liveSyncDurationCount: 1,
          liveMaxLatencyDurationCount: 3,
          maxBufferLength: 3,
          backBufferLength: 0,
          manifestLoadingMaxRetry: 2,
          levelLoadingMaxRetry: 2,
          fragLoadingMaxRetry: 2,
        })
        hls.loadSource(camera.hls_url)
        hls.attachMedia(video)
        hls.on(Hls.Events.MANIFEST_PARSED, () => { void video.play().catch(() => undefined) })
        hls.on(Hls.Events.ERROR, (_event: string, data: { fatal?: boolean }) => {
          if (data.fatal && !destroyed) {
            update({ error: true, live: false, connecting: false, message: 'HLS không khả dụng' })
          }
        })
      } catch (error) {
        if (!destroyed) {
          update({ error: true, live: false, connecting: false, message: `Kết nối HLS lỗi: ${String(error)}` })
        }
      }
    }

    const onPlaying = () => update({ live: true, error: false, connecting: false, transport: 'hls-fallback', message: 'HLS' })
    video.addEventListener('playing', onPlaying)

    const activateFallback = () => {
      if (destroyed) return
      reader?.close()
      reader = null
      readerRef.current = null
      update({ transport: 'hls-fallback', connecting: true, message: 'Đang kết nối HLS' })
      void startFallback()
    }

    const updateStats = () => {
      if (!reader) return
      void reader.getStats().then((stats) => {
        type VideoStats = {
          type?: string
          kind?: string
          mediaType?: string
          jitterBufferEmittedCount?: number
          jitterBufferDelay?: number
        }
        const inbound = Array.from(stats.values())
          .map((report) => report as VideoStats)
          .find((report) => report.type === 'inbound-rtp' && (report.kind === 'video' || report.mediaType === 'video'))
        if (!inbound) return
        const emitted = Number(inbound.jitterBufferEmittedCount)
        const delay = Number(inbound.jitterBufferDelay)
        update({ jitterBufferDelayMs: emitted > 0 && Number.isFinite(delay) ? delay * 1000 / emitted : null })
      }).catch(() => undefined)
    }

    try {
      if (!window.MediaMTXWebRTCReader) throw new Error('MediaMTXWebRTCReader unavailable')
      reader = new window.MediaMTXWebRTCReader({
        url: camera.webrtc_url,
        user: '',
        pass: '',
        token: '',
        onError: () => update({ live: false, connecting: true, error: false, message: 'WebRTC reconnecting...' }),
        onTrack: (event) => {
          if (destroyed || event.track.kind !== 'video') return
          if (fallbackTimer !== null) window.clearTimeout(fallbackTimer)
          fallbackCleanup?.()
          fallbackCleanup = null
          update({ live: true, error: false, connecting: false, transport: 'webrtc', message: 'Live / WebRTC' })
          video.srcObject = event.streams[0]
          void video.play().catch(() => undefined)
        },
      })
      readerRef.current = reader
      statsTimer = window.setInterval(updateStats, 1000)
      fallbackTimer = window.setTimeout(activateFallback, 10000)
    } catch {
      fallbackTimer = window.setTimeout(activateFallback, 0)
    }

    return () => {
      destroyed = true
      if (fallbackTimer !== null) window.clearTimeout(fallbackTimer)
      if (statsTimer !== null) window.clearInterval(statsTimer)
      fallbackCleanup?.()
      hls?.destroy()
      reader?.close()
      readerRef.current = null
      video.removeEventListener('playing', onPlaying)
      video.srcObject = null
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
  }, [camera.hls_url, camera.id, camera.running, camera.webrtc_url, camera.worker_ready, onStateChange])

  return { videoRef, state }
}
