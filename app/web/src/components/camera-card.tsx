import { useCallback } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useMediaStream } from '@/hooks/use-media-stream'
import type { CameraDetail, PlayerState } from '@/types'

interface CameraCardProps {
  camera: CameraDetail
  onStateChange: (cameraId: string, state: PlayerState) => void
}
export function CameraCard({ camera, onStateChange }: CameraCardProps) {
  const handleStateChange = useCallback((state: PlayerState) => {
    onStateChange(camera.id, state)
  }, [camera.id, onStateChange])
  const { videoRef, state } = useMediaStream(camera, handleStateChange)
  const ready = Boolean(camera.worker_ready ?? camera.ready)

  return (
    <Card className="overflow-hidden bg-card/80">
      <CardHeader className="flex-row items-center justify-between space-y-0 border-b px-4 py-3">
        <div className="min-w-0">
          <CardTitle className="truncate text-base">{camera.id}</CardTitle>
          <p className="text-xs text-muted-foreground">
            {camera.running ? `worker ${camera.pid ?? '--'}` : 'worker stopped'}
          </p>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant={state.live ? 'default' : ready ? 'secondary' : 'outline'}>
              {state.live ? state.transport === 'webrtc' ? 'LIVE' : 'HLS' : ready ? 'CONNECTING' : 'OFFLINE'}
            </Badge>
          </TooltipTrigger>
          <TooltipContent>{state.message}</TooltipContent>
        </Tooltip>
      </CardHeader>
      <CardContent className="p-0">
        <div className="relative aspect-video bg-black">
          <video ref={videoRef} className="h-full w-full object-contain" hidden={!state.live} autoPlay muted playsInline />
          {!state.live && (
            <div className="absolute inset-0 grid place-items-center px-4 text-center text-sm text-muted-foreground">
              {camera.running && ready ? state.message : 'Runtime offline'}
            </div>
          )}
        </div>
        <div className="flex items-center justify-between px-4 py-2 text-xs text-muted-foreground">
          <span>{state.transport === 'hls-fallback' ? 'HLS fallback' : 'WebRTC'}</span>
          <span>{state.jitterBufferDelayMs == null ? '--' : `${state.jitterBufferDelayMs.toFixed(0)} ms`}</span>
        </div>
      </CardContent>
    </Card>
  )
}
