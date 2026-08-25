import { CheckCircle2, CircleDashed, Radio, WifiOff } from 'lucide-react'
import type { CameraDetail, PlayerState } from '@/types'

interface CameraStatusBadgeProps {
  camera: CameraDetail
  state: PlayerState
}

export function CameraStatusBadge({ camera, state }: CameraStatusBadgeProps) {
  const ready = Boolean(camera.worker_ready ?? camera.ready)
  const tone = state.live ? 'healthy' : state.error || !camera.running ? 'danger' : 'warning'
  const label = state.live ? (state.transport === 'mock-file' ? 'MOCK' : state.transport === 'webrtc' ? 'LIVE' : 'HLS') : state.error || !camera.running ? 'OFFLINE' : 'CONNECTING'
  const Icon = state.live ? (state.transport === 'hls-fallback' ? Radio : CheckCircle2) : state.error || !camera.running ? WifiOff : CircleDashed

  return (
    <span className={`camera-status camera-status-${tone}`} title={state.message}>
      <Icon size={13} aria-hidden="true" />
      <span>{label}</span>
      {!state.live && ready && !state.error && <span className="sr-only">Worker đã sẵn sàng</span>}
    </span>
  )
}
