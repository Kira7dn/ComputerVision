import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { EventRecord } from '@/types'

interface EventPanelProps {
  events: EventRecord[]
}
function formatTime(timestamp?: number) {
  if (timestamp == null) return '--:--:--'
  return new Date(timestamp * 1000).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function severityVariant(severity?: string) {
  if (severity === 'danger') return 'destructive' as const
  if (severity === 'warning') return 'secondary' as const
  return 'outline' as const
}

export function EventPanel({ events }: EventPanelProps) {
  const [selected, setSelected] = useState<EventRecord | null>(null)
  return (
    <>
      <Card className="flex min-h-0 flex-1 flex-col overflow-hidden bg-card/80">
        <CardHeader className="flex-row items-center justify-between space-y-0 px-4 py-3">
          <CardTitle className="text-base">Danh sách sự kiện</CardTitle>
          <Badge variant="secondary">{events.length}</Badge>
        </CardHeader>
        <Separator />
        <CardContent className="min-h-0 flex-1 p-0">
          <ScrollArea className="h-full">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Camera</TableHead>
                  <TableHead>Sự kiện</TableHead>
                  <TableHead className="text-right">Score</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.length === 0 ? (
                  <TableRow><TableCell colSpan={3} className="h-24 text-center text-muted-foreground">Chưa có sự kiện</TableCell></TableRow>
                ) : events.map((event) => {
                  const id = String(event.event_id ?? `${event.camera}-${event.timestamp}-${event.event_name}`)
                  return (
                    <TableRow key={id} className="cursor-pointer" tabIndex={0} onClick={() => setSelected(event)} onKeyDown={(keyEvent) => { if (keyEvent.key === 'Enter' || keyEvent.key === ' ') setSelected(event) }}>
                      <TableCell className="max-w-28 truncate align-top font-medium">
                        <div>{event.camera ?? 'unknown'}</div>
                        <div className="text-xs text-muted-foreground">{formatTime(event.timestamp)}</div>
                      </TableCell>
                      <TableCell className="align-top">
                        <div className="flex items-center gap-2">
                          {event.thumbnail_url && <img className="h-10 w-16 rounded object-cover" src={event.thumbnail_url} alt="" loading="lazy" />}
                          <div className="min-w-0">
                            <Badge variant={severityVariant(event.severity)}>{event.event_name ?? 'Event'}</Badge>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell className="text-right align-top tabular-nums">{event.confidence == null ? '--' : `${(Number(event.confidence) * 100).toFixed(0)}%`}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </ScrollArea>
        </CardContent>
      </Card>
      <Dialog open={selected !== null} onOpenChange={(open) => { if (!open) setSelected(null) }}>
        <DialogContent className="max-h-[94vh] max-w-3xl overflow-y-auto">
          <DialogHeader><DialogTitle>{selected?.camera ?? 'unknown'} · {selected?.event_name ?? 'Event'}</DialogTitle></DialogHeader>
          {selected && (
            <div className="space-y-4">
              {selected.image_url || selected.thumbnail_url
                ? <img className="max-h-[430px] w-full rounded object-contain" src={selected.image_url ?? selected.thumbnail_url ?? undefined} alt="Event thumbnail" />
                : <div className="grid min-h-32 place-items-center rounded bg-muted text-muted-foreground">Không có thumbnail</div>}
              <div className="grid gap-2 sm:grid-cols-2">
                {[
                  ['Camera', selected.camera ?? '--'],
                  ['Thời gian', formatTime(selected.timestamp)],
                  ['Phân loại', selected.classification ?? '--'],
                  ['Danh tính', selected.name ?? '--'],
                  ['Độ tin cậy', selected.confidence == null ? '--' : `${(Number(selected.confidence) * 100).toFixed(2)}%`],
                  ['Frame bắt đầu', String(selected.details?.recognition_frame_number ?? selected.start_record?.frame ?? '--')],
                ].map(([label, value]) => <div key={label} className="rounded border bg-muted/30 p-2"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 break-words text-sm">{value}</div></div>)}
              </div>
              <pre className="overflow-auto rounded bg-muted p-3 text-xs">{JSON.stringify({ event: selected, details: selected.details ?? {}, start_record: selected.start_record ?? {} }, null, 2)}</pre>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
