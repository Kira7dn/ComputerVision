import { useCallback } from 'react'
import { Activity, BrainCircuit, Clock3, Cpu, Flame, ScanFace, Cigarette } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useMediaStream } from '@/hooks/use-media-stream'
import { CameraStatusBadge } from '@/components/camera-status-badge'
import type { CameraDetail, PlayerState } from '@/types'

interface CameraCardProps {
  camera: CameraDetail
  focused: boolean
  onFocus: (cameraId: string) => void
  onStateChange: (cameraId: string, state: PlayerState) => void
}
export function CameraCard({ camera, focused, onFocus, onStateChange }: CameraCardProps) {
  const handleStateChange = useCallback((state: PlayerState) => {
    onStateChange(camera.id, state)
  }, [camera.id, onStateChange])
  const { videoRef, state } = useMediaStream(camera, handleStateChange)
  const ready = Boolean(camera.worker_ready ?? camera.ready)
  const displayName = camera.display_name ?? friendlyCameraName(camera.id)
  const isDms = camera.id === 'DMS' || Boolean(camera.functions?.dms)
  const functions = Object.entries(camera.functions ?? {}).filter(([name, enabled]) => enabled && !(isDms && name === 'dms'))
  const latencyTitle = state.videoLatencySource === 'rtp_ntp_map'
    ? 'Đối chiếu RTP frame với timestamp NTP của Dahua đến thời điểm compositor'
    : 'Ước tính từ captureTime của camera đến thời điểm browser đưa frame lên compositor'

  return (
    <Card
      className={`camera-card group gap-0 overflow-hidden bg-card/80 transition hover:-translate-y-px hover:border-healthy/35 hover:shadow-[0_15px_38px_rgba(0,0,0,0.23)] md:min-h-0 py-1 ${focused ? 'camera-card-focused' : ''}`}
      role="button"
      tabIndex={0}
      aria-pressed={focused}
      aria-label={`${focused ? 'Camera chính' : 'Chọn làm camera chính'} ${displayName}`}
      onClick={() => onFocus(camera.id)}
      onKeyDown={(event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        onFocus(camera.id)
      }}
    >
      <CardHeader className="flex flex-row flex-nowrap items-center justify-between gap-1 space-y-0 overflow-hidden border-b px-2 pt-2 pb-2!">
        <div className="flex h-full min-w-0 flex-1 items-center gap-2">
          <CardTitle className="min-w-0 truncate text-sm leading-none">{displayName}</CardTitle>
          {!isDms && <span className="max-w-28 truncate font-mono text-[0.62rem] leading-none text-muted-foreground max-[430px]:hidden">{camera.id}</span>}
          <span className="inline-flex shrink-0 items-center gap-[0.2rem] whitespace-nowrap font-mono text-[0.6rem] leading-none text-muted-foreground"><Cpu size={11} /> {camera.media_only ? 'media only' : camera.running ? `worker ${camera.pid ?? '--'}` : 'worker offline'}</span>
          {camera.analysis_error && <span className="shrink-0 whitespace-nowrap text-[0.62rem] leading-none text-danger" title={camera.analysis_error}>detector lỗi</span>}
        </div>
        <div className="flex h-full shrink-0 items-center"><CameraStatusBadge camera={camera} state={state} /></div>
      </CardHeader>
      <CardContent className="p-0 md:flex md:min-h-0 md:flex-1 md:flex-col">
        <div className="relative aspect-video overflow-hidden bg-[linear-gradient(135deg,_#0d1117,_#151b24)] p-0 md:min-h-0 md:flex-1 md:aspect-auto">
          <video ref={videoRef} className="h-full w-full bg-black object-contain p-0" hidden={!state.live} autoPlay muted playsInline aria-label={`Luồng video ${displayName}`} />
          {!state.live && <CameraEmptyState camera={camera} state={state} ready={ready} />}
          <div className="absolute inset-x-0 bottom-0 z-[2] flex min-w-0 items-center justify-between gap-[0.7rem] text-[0.66rem] [text-shadow:0_1px_3px_#000] max-[430px]:flex-col max-[430px]:items-end max-[430px]:gap-[0.35rem]">
            <div className="flex min-w-0 flex-wrap gap-[0.3rem]" aria-label="Chức năng camera">
              {functions.length ? functions.map(([name]) => <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-full border border-white/20 bg-black/50 px-[0.38rem] py-[0.2rem] text-white/90" key={name}>{functionIcon(name)} {functionLabel(name)}</span>) : !isDms && <span className="text-muted-foreground">Chưa khai báo chức năng</span>}
            </div>
            <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap font-mono text-white/90" title={latencyTitle}><Clock3 size={12} /> {state.videoLatencyMs == null ? '--' : `${state.videoLatencyMs.toFixed(0)} ms`}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function friendlyCameraName(id: string) {
  const names: Record<string, string> = {
    camera_face: 'Cổng nhận diện',
    camera_safety: 'Khu vực an toàn',
    camera_front: 'Camera trước',
    camera_back: 'Camera sau',
    camera_left: 'Camera trái',
    camera_right: 'Camera phải',
    DMS: 'DMS',
  }
  return names[id] ?? id.replace(/^camera_/, '').replaceAll('_', ' ')
}

function functionLabel(name: string) {
  const labels: Record<string, string> = {
    face_recognition: 'Khuôn mặt',
    smoking_behavior: 'Hút thuốc',
    fire_smoke: 'Lửa / khói',
    lpr: 'Biển số',
  }
  return labels[name] ?? name.replaceAll('_', ' ')
}

function functionIcon(name: string) {
  if (name.includes('face')) return <ScanFace size={12} />
  if (name.includes('smok')) return <Cigarette size={12} />
  if (name.includes('fire')) return <Flame size={12} />
  return <BrainCircuit size={12} />
}

function CameraEmptyState({ camera, state, ready }: { camera: CameraDetail; state: PlayerState; ready: boolean }) {
  const title = !camera.running ? 'Worker đang offline' : state.error ? 'Không nhận được luồng' : ready ? 'Đang kết nối video' : 'Đang chờ worker'
  const message = !camera.running ? 'Kiểm tra runtime hoặc cấu hình camera.' : state.message
  return (
    <div className="absolute inset-0 grid place-items-center content-center gap-[0.35rem] p-0 text-center text-muted-foreground" role="status">
      <div className={`grid size-10 place-items-center rounded-xl bg-white/5 ${state.error || !camera.running ? 'text-danger' : 'text-warning'}`}><Activity size={22} /></div>
      <strong className="text-sm text-foreground">{title}</strong>
      <span className="max-w-72 text-[0.72rem]">{message}</span>
      {ready && !state.error && camera.running && <span className="mt-[0.3rem] h-[0.22rem] w-28 animate-pulse rounded-full bg-warning/50" aria-hidden="true" />}
    </div>
  )
}
