import { useState, type ReactNode } from 'react'
import { Activity, Inbox, RefreshCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { EventDetailMetadata } from '@/components/event-detail-metadata'
import { EventListItem } from '@/components/event-list-item'
import type { EventRecord } from '@/types'

interface EventPanelProps {
  events: EventRecord[]
  loading?: boolean
  error?: boolean
}

export function EventPanel({ events, loading = false, error = false }: EventPanelProps) {
  const [selected, setSelected] = useState<EventRecord | null>(null)

  return (
    <>
      <Card className="event-panel flex min-h-0 flex-1 flex-col gap-0 overflow-hidden bg-card/80">
        <CardHeader className="flex flex-row flex-nowrap items-center justify-between gap-0 space-y-0 rounded-t-xl px-2 py-1 [&.border-b]:pb-0">
          <div className="flex h-full min-w-0 items-center gap-2">
            <span className="panel-icon"><Activity size={15} /></span>
            <div className="min-w-0">
              <CardTitle className="text-sm leading-none">Sự kiện mới nhất</CardTitle>
            </div>
          </div>
          <Badge className="self-center" variant="secondary">{events.length}</Badge>
        </CardHeader>
        <Separator />
        <CardContent className="min-h-0 flex-1 p-0">
          <ScrollArea className="h-full">
            <div className="event-list" aria-live="polite">
              {loading && events.length === 0 && <EventPanelState icon={<RefreshCw className="animate-spin" size={18} />} title="Đang tải sự kiện" message="Đang đồng bộ event feed…" />}
              {!loading && error && events.length === 0 && <EventPanelState icon={<RefreshCw size={18} />} title="Không kết nối được event feed" message="Giữ danh sách cũ và sẽ thử lại tự động." tone="warning" />}
              {!loading && !error && events.length === 0 && <EventPanelState icon={<Inbox size={20} />} title="Chưa có sự kiện" message="Các event START mới sẽ xuất hiện ở đây." />}
              {events.map((event) => <EventListItem key={String(event.event_id ?? `${event.camera}-${event.timestamp}-${event.event_name}`)} event={event} onSelect={setSelected} />)}
            </div>
          </ScrollArea>
        </CardContent>
      </Card>

      <Dialog open={selected !== null} onOpenChange={(open) => { if (!open) setSelected(null) }}>
        <DialogContent className="event-dialog max-h-[94vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{selected?.camera ?? 'Camera không xác định'} · {selected?.event_name ?? 'Sự kiện'}</DialogTitle>
          </DialogHeader>
          {selected && (
            <div className="space-y-5">
              {selected.image_url || selected.thumbnail_url
                ? <img className="event-evidence-image" src={selected.image_url ?? selected.thumbnail_url ?? undefined} alt={`Evidence ${selected.camera ?? 'camera'}`} />
                : <div className="event-evidence-empty">Không có ảnh evidence</div>}
              <EventDetailMetadata event={selected} />
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}

function EventPanelState({ icon, title, message, tone = 'neutral' }: { icon: ReactNode; title: string; message: string; tone?: 'neutral' | 'warning' }) {
  return <div className={`event-panel-state event-panel-state-${tone}`} role="status"><span>{icon}</span><strong>{title}</strong><p>{message}</p></div>
}
