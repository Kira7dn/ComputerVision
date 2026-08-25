import { useEffect, useRef, useState } from 'react'
import type { CameraDetail, HlsConstructor, HlsInstance, MediaMTXReader, PlayerState } from '@/types'

interface VideoFrameMetadataLike {
  captureTime?: number
  presentationTime?: number
  expectedDisplayTime?: number
  rtpTimestamp?: number
}

interface FrameTimingSample {
  rtp_timestamp: number
  capture_timestamp: number
  output_timestamp: number
}

interface LiveMetadataResponse {
  timestamp?: number
  cameras?: Record<string, { frame_timing_samples?: FrameTimingSample[] }>
}

type VideoFrameCallback = (now: number, metadata: VideoFrameMetadataLike) => void

type VideoWithFrameCallback = {
  requestVideoFrameCallback?: (callback: VideoFrameCallback) => number
  cancelVideoFrameCallback?: (handle: number) => void
}

const initialState: PlayerState = {
  live: false,
  error: false,
  connecting: true,
  transport: 'webrtc',
  videoLatencyMs: null,
  videoLatencySource: 'unavailable',
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
    let videoFrameCallbackId: number | null = null
    let frameTimingTimer: number | null = null
    let lastLatencyReportAt = 0
    let lastValidLatencyAt = 0
    let smoothedVideoLatencyMs: number | null = null
    let serverBrowserClockOffsetMs: number | null = null
    let rtpTimestampOffset: number | null = null
    let frameTimingRequestInFlight = false
    const frameSamples = new Map<number, FrameTimingSample>()
    const pendingFrames = new Map<number, number>()
    let destroyed = false
    const timedVideo = video as unknown as VideoWithFrameCallback

    const update = (next: Partial<PlayerState>) => {
      if (!destroyed) setState((current) => ({ ...current, ...next }))
    }

    const updateLatency = (rawLatencyMs: number, source: 'webrtc_capture' | 'rtp_ntp_map') => {
      if (!Number.isFinite(rawLatencyMs) || rawLatencyMs < 0 || rawLatencyMs > 30_000) return
      smoothedVideoLatencyMs = smoothedVideoLatencyMs == null
        ? rawLatencyMs
        : smoothedVideoLatencyMs * 0.8 + rawLatencyMs * 0.2
      const reportNow = performance.now()
      if (reportNow - lastLatencyReportAt < 500) return
      lastLatencyReportAt = reportNow
      lastValidLatencyAt = reportNow
      update({ videoLatencyMs: smoothedVideoLatencyMs, videoLatencySource: source })
    }

    const displayLatencyForSample = (displayTime: number, sample: FrameTimingSample) => {
      if (serverBrowserClockOffsetMs == null) return
      const displayEpochMs = performance.timeOrigin + displayTime
      const captureEpochInBrowserMs = sample.capture_timestamp * 1000 - serverBrowserClockOffsetMs
      const rawLatencyMs = displayEpochMs - captureEpochInBrowserMs
      const cameraPipelineLatencyMs = (sample.output_timestamp - sample.capture_timestamp) * 1000
      // A frame shown in the browser cannot arrive earlier than the same
      // frame's measured camera-to-DeepStream path. Reject a bad RTP offset or
      // an unsynchronised clock instead of displaying a plausible false value.
      if (
        Number.isFinite(cameraPipelineLatencyMs)
        && rawLatencyMs < cameraPipelineLatencyMs - 5
      ) return
      updateLatency(rawLatencyMs, 'rtp_ntp_map')
    }

    const refreshFrameTiming = async () => {
      if (destroyed || frameTimingRequestInFlight) return
      frameTimingRequestInFlight = true
      const requestStart = performance.now()
      try {
        const response = await fetch('/api/live-metadata', { cache: 'no-store' })
        if (!response.ok || destroyed) return
        const payload = await response.json() as LiveMetadataResponse
        const requestEnd = performance.now()
        const serverTimestamp = Number(payload.timestamp)
        if (Number.isFinite(serverTimestamp)) {
          const browserMidpointEpochMs = performance.timeOrigin + (requestStart + requestEnd) / 2
          const offset = serverTimestamp * 1000 - browserMidpointEpochMs
          serverBrowserClockOffsetMs = serverBrowserClockOffsetMs == null
            ? offset
            : serverBrowserClockOffsetMs * 0.8 + offset * 0.2
        }
        const samples = payload.cameras?.[camera.id]?.frame_timing_samples ?? []
        for (const sample of samples) {
          const rtpTimestamp = Number(sample.rtp_timestamp) >>> 0
          if (Number.isFinite(sample.capture_timestamp)) frameSamples.set(rtpTimestamp, sample)
        }
        if (rtpTimestampOffset == null && pendingFrames.size >= 2 && samples.length >= 2) {
          const candidateCounts = new Map<number, number>()
          for (const rtpTimestamp of pendingFrames.keys()) {
            for (const sample of samples) {
              const candidate = (rtpTimestamp - (Number(sample.rtp_timestamp) >>> 0)) >>> 0
              candidateCounts.set(candidate, (candidateCounts.get(candidate) ?? 0) + 1)
            }
          }
          const best = [...candidateCounts.entries()].sort((left, right) => right[1] - left[1])[0]
          // Two coincidental pairs are possible when the browser is outside
          // the server ring. Require a longer sequence before trusting it.
          if (best && best[1] >= 5) rtpTimestampOffset = best[0]
        }
        for (const [rtpTimestamp, displayTime] of pendingFrames) {
          const mappedRtpTimestamp = rtpTimestampOffset == null
            ? null
            : (rtpTimestamp - rtpTimestampOffset) >>> 0
          const sample = mappedRtpTimestamp == null ? undefined : frameSamples.get(mappedRtpTimestamp)
          if (!sample) continue
          displayLatencyForSample(displayTime, sample)
          pendingFrames.delete(rtpTimestamp)
        }
        while (pendingFrames.size > 180) {
          const oldest = pendingFrames.keys().next().value
          if (oldest === undefined) break
          pendingFrames.delete(oldest)
        }
        if (lastValidLatencyAt > 0 && performance.now() - lastValidLatencyAt > 2_000) {
          update({ videoLatencyMs: null, videoLatencySource: 'unavailable' })
        }
      } catch {
        // Keep the last measured value during a transient metadata request.
      } finally {
        frameTimingRequestInFlight = false
      }
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

    const onPlaying = () => {
      if (reader !== null) return
      update({
        live: true,
        error: false,
        connecting: false,
        transport: 'hls-fallback',
        videoLatencyMs: null,
        videoLatencySource: 'unavailable',
        message: 'HLS',
      })
    }
    video.addEventListener('playing', onPlaying)

    const stopFrameTiming = () => {
      if (videoFrameCallbackId !== null && timedVideo.cancelVideoFrameCallback) {
        timedVideo.cancelVideoFrameCallback(videoFrameCallbackId)
      }
      videoFrameCallbackId = null
      if (frameTimingTimer !== null) window.clearInterval(frameTimingTimer)
      frameTimingTimer = null
      smoothedVideoLatencyMs = null
      lastLatencyReportAt = 0
      lastValidLatencyAt = 0
      serverBrowserClockOffsetMs = null
      rtpTimestampOffset = null
      frameSamples.clear()
      pendingFrames.clear()
    }

    const onVideoFrame: VideoFrameCallback = (callbackNow, metadata) => {
      if (destroyed) return
      const captureTime = Number(metadata.captureTime)
      if (Number.isFinite(captureTime)) {
        // expectedDisplayTime is the browser's best estimate of when the
        // frame reaches the compositor. presentationTime is the safe fallback
        // on browsers that do not expose the expected display deadline.
        const displayTimeValue = Number(
          metadata.expectedDisplayTime ?? metadata.presentationTime ?? callbackNow,
        )
        updateLatency(displayTimeValue - captureTime, 'webrtc_capture')
      } else {
        const rtpTimestamp = Number(metadata.rtpTimestamp)
        if (Number.isFinite(rtpTimestamp)) {
          const normalizedRtpTimestamp = rtpTimestamp >>> 0
          pendingFrames.set(normalizedRtpTimestamp, Number(
            metadata.expectedDisplayTime ?? metadata.presentationTime ?? callbackNow,
          ))
          const sample = frameSamples.get(normalizedRtpTimestamp)
          if (sample) {
            displayLatencyForSample(pendingFrames.get(normalizedRtpTimestamp) ?? callbackNow, sample)
            pendingFrames.delete(normalizedRtpTimestamp)
          }
        }
      }
      if (!destroyed && timedVideo.requestVideoFrameCallback) {
        videoFrameCallbackId = timedVideo.requestVideoFrameCallback(onVideoFrame)
      }
    }

    const startFrameTiming = () => {
      stopFrameTiming()
      if (timedVideo.requestVideoFrameCallback) {
        videoFrameCallbackId = timedVideo.requestVideoFrameCallback(onVideoFrame)
      }
      frameTimingTimer = window.setInterval(() => void refreshFrameTiming(), 250)
      void refreshFrameTiming()
    }

    const activateFallback = () => {
      if (destroyed) return
      reader?.close()
      reader = null
      readerRef.current = null
      stopFrameTiming()
      update({ transport: 'hls-fallback', connecting: true, message: 'Đang kết nối HLS' })
      void startFallback()
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
          update({
            live: true,
            error: false,
            connecting: false,
            transport: 'webrtc',
            videoLatencyMs: null,
            videoLatencySource: 'unavailable',
            message: timedVideo.requestVideoFrameCallback
              ? 'Live / WebRTC · đo màn hình'
              : 'Live / WebRTC · browser không hỗ trợ đo frame',
          })
          video.srcObject = event.streams[0]
          void video.play().catch(() => undefined)
          startFrameTiming()
        },
      })
      readerRef.current = reader
      fallbackTimer = window.setTimeout(activateFallback, 10000)
    } catch {
      fallbackTimer = window.setTimeout(activateFallback, 0)
    }

    return () => {
      destroyed = true
      if (fallbackTimer !== null) window.clearTimeout(fallbackTimer)
      stopFrameTiming()
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
