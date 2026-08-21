import { AlertTriangle, Cigarette, Flame, ScanFace, ShieldAlert, UserRound } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { EventRecord } from '@/types'

interface EventListItemProps {
  event: EventRecord
  onSelect: (event: EventRecord) => void
}

export function formatEventTime(timestamp?: number) {
  if (timestamp == null) return '--:--:--'
  return new Date(timestamp * 1000).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function eventLabel(event: EventRecord) {
  if (event.severity_label && event.severity_label !== 'Sự kiện') return event.severity_label
  const labels: Record<string, string> = {
    smoking: 'Hút thuốc',
    smoking_behavior: 'Hành vi hút thuốc',
    fire: 'Lửa',
    smoke: 'Khói',
    recognized: 'Đã nhận diện',
    unrecognized: 'Không nhận diện được',
  }
  return labels[event.classification ?? ''] ?? event.event_name ?? 'Sự kiện'
}

function eventTone(event: EventRecord) {
  if (event.severity === 'danger' || event.classification === 'fire') return 'danger'
  if (event.severity === 'warning' || ['smoke', 'smoking', 'smoking_behavior'].includes(event.classification ?? '')) return 'warning'
  return 'event'
}

function eventIcon(event: EventRecord) {
  if (event.classification === 'fire') return <Flame size={15} />
  if (['smoke', 'smoking', 'smoking_behavior'].includes(event.classification ?? '')) return <Cigarette size={15} />
  if (event.function === 'face_recognition') return <ScanFace size={15} />
  if (event.classification === 'recognized' || event.classification === 'unrecognized') return <UserRound size={15} />
  return event.severity === 'danger' ? <ShieldAlert size={15} /> : <AlertTriangle size={15} />
}

export function EventListItem({ event, onSelect }: EventListItemProps) {
  const tone = eventTone(event)
  const confidence = event.confidence == null ? null : `${(Number(event.confidence) * 100).toFixed(0)}%`
  const title = `${event.camera ?? 'camera không xác định'} · ${eventLabel(event)}`

  return (
    <button type="button" className={`event-list-item event-list-item-${tone}`} onClick={() => onSelect(event)} aria-label={`Mở chi tiết ${title}`}>
      <span className={`event-icon event-icon-${tone}`} aria-hidden="true">{eventIcon(event)}</span>
      <span className="event-thumb-wrap">
        {event.thumbnail_url ? <img className="event-thumb" src={event.thumbnail_url} alt="" loading="lazy" /> : <span className="event-thumb event-thumb-empty"><ShieldAlert size={16} /></span>}
      </span>
      <span className="event-list-copy">
        <span className="event-list-topline">
          <strong className="truncate">{event.camera ?? 'Camera không xác định'}</strong>
          <time dateTime={event.timestamp ? new Date(event.timestamp * 1000).toISOString() : undefined}>{formatEventTime(event.timestamp)}</time>
        </span>
        <span className="event-list-bottomline">
          <Badge className={`event-badge event-badge-${tone}`} variant="outline">{eventLabel(event)}</Badge>
          {event.name && event.name !== 'unknown' && <span className="truncate text-muted-foreground">{event.name}</span>}
          {confidence && <span className="event-confidence">{confidence}</span>}
        </span>
      </span>
    </button>
  )
}
