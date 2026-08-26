export interface CameraDetail {
  id: string
  display_name?: string
  source?: string
  source_type?: 'rtsp' | 'mock' | string
  media_only?: boolean
  media_url?: string | null
  mock_sync_group?: string | null
  mock_sync_period_seconds?: number
  mock_sync_epoch_seconds?: number
  running: boolean
  pid: number | null
  ready: boolean
  worker_ready?: boolean
  webrtc_url: string
  hls_url: string
  functions?: Record<string, boolean>
  last_frame_age_seconds?: number | null
  last_output_age_seconds?: number | null
  camera_latency_ms?: number | null
  camera_source_timestamp?: number | null
  camera_latency_source?: 'rtcp_ntp' | 'unavailable' | string
  camera_latency_samples?: number
  rss_mb?: number | null
  input_decoder?: string | null
  output_encoder?: string | null
  analysis_error?: string | null
  driver_attention?: {
    contract_version?: number
    readiness?: 'warming' | 'ready' | 'degraded' | 'not_ready' | 'disabled' | string
    state?: 'attentive' | 'distracted' | 'unknown' | 'no_driver' | 'warming' | string
    score?: number | null
    alert_level?: 'none' | 'warning' | 'critical' | 'emergency' | string
    reasons?: string[]
    source?: string
    pose_calibrated_percent?: number
    model_uncertainty?: number | null
    inference_ms?: number | null
  }
  front_assistance?: {
    contract_version?: number
    mode?: 'vision_only' | string
    readiness?: 'warming' | 'ready' | 'degraded' | 'not_ready' | 'disabled' | string
    blocking_reasons?: string[]
    active_alerts?: string[]
    provider?: string
    inference_ms?: number
    model_hash?: string
    calibration_hash?: string
    overlay?: {
      visible_lane_count?: number
      lane_segment_count?: number
      path_point_count?: number
      path_segment_count?: number
      path_source?: 'model_position' | 'unavailable' | string
      rendered_segment_count?: number
      lane_confidences?: Record<string, number>
    }
  }
}
export interface MetricsResponse {
  timestamp: number
  host: {
    cpu_percent: number | null
    cpu_cores: number | null
    memory: {
      total_mb: number | null
      used_mb: number | null
      percent: number | null
    }
  }
  gpu: {
    available: boolean
    utilization_percent?: number | null
    memory_used_mb?: number | null
    temperature_c?: number | null
  }
  pipeline: {
    running: boolean
    pid: number | null
    pids: number[]
    camera_count: number
    ready: boolean
    cameras: string[]
    cpu_percent: number | null
    rss_mb: number | null
    age_seconds: number | null
    camera_details: CameraDetail[]
    mock_timeline?: {
      schema_version?: number
      ready?: boolean
      fresh?: boolean
      updated_at?: number
      groups?: Record<string, {
        locked?: boolean
        period_seconds?: number
        epoch_seconds?: number
        normalized_phase?: number
        cameras?: Record<string, {
          mode?: 'publisher' | 'direct_file' | string
          ready?: boolean
        }>
      }>
    }
  }
  stream: {
    worker_ready: boolean
    webrtc_url: string | null
    hls_url: string | null
  }
  evidence: Record<string, unknown>
}

export interface EventRecord {
  event_id?: string
  camera?: string
  event_name?: string
  label?: string
  timestamp?: number
  sequence?: number
  confidence?: number | null
  severity?: string
  severity_label?: string
  thumbnail_url?: string | null
  image_url?: string | null
  classification?: string
  name?: string
  function?: string
  state?: string
  lifecycle?: string
  started_at?: number | null
  updated_at?: number | null
  ended_at?: number | null
  record_type?: string
  region_track_id?: number | string | null
  confirmation_state?: string | null
  best_frame_number?: number | null
  best_bbox?: number[] | null
  person_track_id?: number | string | null
  best_person_bbox?: number[] | null
  best_model_roi_bbox?: number[] | null
  detector_hits?: number | null
  positive_votes?: number | null
  observation_window?: number | null
  dynamic_votes?: number | null
  dynamic_score?: number | null
  best_score?: number | null
  classifier_score?: number | null
  object_score?: number | null
  notification_emitted?: boolean | null
  details?: Record<string, unknown>
  start_record?: Record<string, unknown>
  [key: string]: unknown
}

export interface EventsResponse {
  run_id: string | null
  cursor: number
  events: EventRecord[]
}

export type StreamTransport = 'webrtc' | 'hls-fallback' | 'mock-file'

export type VideoLatencySource = 'webrtc_capture' | 'rtp_ntp_map' | 'unavailable'

export interface PlayerState {
  live: boolean
  error: boolean
  connecting: boolean
  transport: StreamTransport
  videoLatencyMs: number | null
  videoLatencySource: VideoLatencySource
  message: string
}

export interface MediaMTXReader {
  getStats(): Promise<RTCStatsReport>
  close(): void
}

export interface MediaMTXReaderOptions {
  url: string
  user: string
  pass: string
  token: string
  onError: (error: string) => void
  onTrack: (event: RTCTrackEvent) => void
}

export interface HlsInstance {
  loadSource(url: string): void
  attachMedia(video: HTMLVideoElement): void
  on(event: string, callback: (_event: string, data: { fatal?: boolean }) => void): void
  destroy(): void
}

export interface HlsConstructor {
  new (options: Record<string, unknown>): HlsInstance
  isSupported(): boolean
  Events: { MANIFEST_PARSED: string; ERROR: string }
}

declare global {
  interface Window {
    MediaMTXWebRTCReader?: new (options: MediaMTXReaderOptions) => MediaMTXReader
    Hls?: HlsConstructor
  }
}
