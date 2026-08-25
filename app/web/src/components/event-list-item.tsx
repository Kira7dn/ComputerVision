import { AlertTriangle, Brain, Cigarette, EyeOff, Flame, ScanFace, ShieldAlert, Smartphone, UserRound, Utensils } from 'lucide-react'
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

const DMS_LABELS: Record<string, string> = {
  smoking: 'Hút thuốc',
  cigarette: 'Hút thuốc',
  drinking: 'Uống nước',
  eating: 'Ăn uống',
  'phone usage': 'Dùng điện thoại',
  phone: 'Dùng điện thoại',
  phoneuse: 'Dùng điện thoại',
  distracted: 'Mất tập trung',
  'driver inattention': 'Tài xế mất tập trung',
  drowsy: 'Buồn ngủ',
  yawning: 'Ngáp',
  'eyes closed': 'Nhắm mắt',
  'head away': 'Quay đầu',
  'no seatbelt': 'Không thắt dây an toàn',
  seatbelt: 'Dây an toàn',
}

function normalizedLabel(value: string) {
  return value
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .trim()
    .toLowerCase()
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
}

function humanizeLabel(value: string) {
  return value
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function stringList(value: unknown) {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
  if (typeof value === 'string' && value.trim() !== '') return [value]
  return []
}

function translateDmsLabel(value: string) {
  return DMS_LABELS[normalizedLabel(value)] ?? humanizeLabel(value)
}

export function dmsMetrics(event: EventRecord) {
  const details = asRecord(event.details)
  return asRecord(details?.dms_metrics) ?? {}
}

export function dmsAlertLabels(event: EventRecord) {
  if (event.function !== 'dms') return []
  const details = asRecord(event.details)
  const eventLabelValue = typeof event.label === 'string'
    ? event.label
    : stringList(details?.dms_alert)[0]
      ?? event.event_name?.replace(/^DMS:\s*/i, '').trim()
      ?? event.classification
  return eventLabelValue ? [translateDmsLabel(eventLabelValue)] : []
}

export function dmsStatusLabel(event: EventRecord) {
  if (event.function !== 'dms') return null
  const details = asRecord(event.details)
  const status = details?.dms_status ?? event.state
  if (typeof status !== 'string' || status.trim() === '') return null
  return status.toUpperCase()
}

export function eventLifecycleLabel(event: EventRecord) {
  if (event.state === 'ended' || event.record_type === 'END') return 'ĐÃ KẾT THÚC'
  if (event.state === 'active' || event.record_type === 'START' || event.record_type === 'UPDATE') return 'ĐANG DIỄN RA'
  return null
}

export function eventSeverityLabel(event: EventRecord) {
  return event.severity_label && event.severity_label !== 'Sự kiện' ? event.severity_label : 'Sự kiện'
}

export function eventScore(event: EventRecord) {
  const details = asRecord(event.details)
  const candidates = [event.confidence, details?.score, details?.last_score]
  for (const value of candidates) {
    const score = typeof value === 'number' ? value : Number(value)
    if (Number.isFinite(score) && score > 0 && score <= 1) return score
  }
  return null
}

export function formatEventScore(event: EventRecord, digits = 0) {
  const score = eventScore(event)
  return score == null ? null : `${(score * 100).toFixed(digits)}%`
}

export function formatDmsEvidence(event: EventRecord) {
  if (event.function !== 'dms') return null
  const details = asRecord(event.details)
  const storedEvidence = asRecord(details?.dms_evidence)
  const metrics = dmsMetrics(event)
  const label = normalizedLabel(event.label ?? event.classification ?? '')
  const yawValue = storedEvidence?.yaw_deg ?? metrics.yaw_deg
  const pitchValue = storedEvidence?.pitch_deg ?? metrics.pitch_deg
  const earValue = storedEvidence?.ear ?? metrics.ear
  const marValue = storedEvidence?.mar ?? metrics.mar
  const yaw = typeof yawValue === 'number' ? yawValue.toFixed(1) : null
  const pitch = typeof pitchValue === 'number' ? pitchValue.toFixed(1) : null
  const ear = typeof earValue === 'number' ? earValue.toFixed(3) : null
  const mar = typeof marValue === 'number' ? marValue.toFixed(3) : null
  if (label === 'driver inattention') {
    const score = storedEvidence?.attention_score
    const reasons = stringList(storedEvidence?.attention_reasons)
    const reasonText = reasons.map((item) => DMS_LABELS[normalizedLabel(item)] ?? humanizeLabel(item)).join(' · ')
    return `Attention ${typeof score === 'number' ? `${score}%` : '--'}${reasonText ? ` · ${reasonText}` : ''}`
  }
  if (label === 'head away' && (yaw !== null || pitch !== null)) return `Yaw ${yaw ?? '--'}° · Pitch ${pitch ?? '--'}°`
  if (label === 'no seatbelt') {
    const hits = storedEvidence?.confirmation_hits
    const required = storedEvidence?.required_hits
    return typeof hits === 'number' && typeof required === 'number'
      ? `Không thấy dây · ${hits}/${required} khung`
      : 'Không phát hiện dây an toàn'
  }
  if (label === 'eyes closed' && ear !== null) return `EAR ${ear}`
  if (label === 'yawning' && mar !== null) return `MAR ${mar}`
  if (yaw !== null || pitch !== null) return `Yaw ${yaw ?? '--'}° · Pitch ${pitch ?? '--'}°`
  if (ear !== null || mar !== null) return `EAR ${ear ?? '--'} · MAR ${mar ?? '--'}`
  return metrics.face_detected === true ? 'FaceMesh đã xác nhận' : null
}

export function eventLabel(event: EventRecord) {
  if (event.function === 'dms') {
    const alert = dmsAlertLabels(event)[0]
    if (alert) return alert
    const rawName = event.event_name?.replace(/^DMS:\s*/i, '').trim()
    if (rawName && normalizedLabel(rawName) !== 'dms') return translateDmsLabel(rawName)
  }
  const labels: Record<string, string> = {
    smoking: 'Hút thuốc',
    smoking_behavior: 'Hành vi hút thuốc',
    fire: 'Lửa',
    smoke: 'Khói',
    recognized: 'Đã nhận diện',
    unrecognized: 'Không nhận diện được',
  }
  return event.event_name ?? labels[event.classification ?? ''] ?? 'Sự kiện'
}

function eventTone(event: EventRecord) {
  if (event.severity === 'danger' || event.classification === 'fire') return 'danger'
  if (event.severity === 'warning' || ['smoke', 'smoking', 'smoking_behavior'].includes(event.classification ?? '')) return 'warning'
  return 'event'
}

function eventIcon(event: EventRecord) {
  const alert = normalizedLabel(dmsAlertLabels(event)[0] ?? '')
  if (event.function === 'dms') {
    if (alert.includes('smoking') || alert.includes('cigarette')) return <Cigarette size={15} />
    if (alert.includes('phone')) return <Smartphone size={15} />
    if (alert.includes('head away') || alert.includes('drowsy') || alert.includes('yawning') || alert.includes('eyes closed')) return <EyeOff size={15} />
    if (alert.includes('distracted')) return <Brain size={15} />
    if (alert.includes('drinking') || alert.includes('eating')) return <Utensils size={15} />
    if (alert.includes('seatbelt')) return <ShieldAlert size={15} />
  }
  if (event.classification === 'fire') return <Flame size={15} />
  if (['smoke', 'smoking', 'smoking_behavior'].includes(event.classification ?? '')) return <Cigarette size={15} />
  if (event.function === 'face_recognition') return <ScanFace size={15} />
  if (event.classification === 'recognized' || event.classification === 'unrecognized') return <UserRound size={15} />
  return event.severity === 'danger' ? <ShieldAlert size={15} /> : <AlertTriangle size={15} />
}

export function EventListItem({ event, onSelect }: EventListItemProps) {
  const tone = eventTone(event)
  const confidence = formatEventScore(event)
  const evidence = confidence == null ? formatDmsEvidence(event) : null
  const status = dmsStatusLabel(event)
  const lifecycle = eventLifecycleLabel(event)
  const severity = eventSeverityLabel(event)
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
        <span className="event-list-alertline">
          <strong className="event-alert-title truncate">{eventLabel(event)}</strong>
          <Badge className={`event-badge event-badge-${tone}`} variant="outline">{severity}</Badge>
        </span>
        <span className="event-list-bottomline">
          {event.name && event.name !== 'unknown' && <span className="truncate text-muted-foreground">{event.name}</span>}
          {status && <span className="event-status">{status}</span>}
          {lifecycle && <span className={`event-lifecycle event-lifecycle-${event.state === 'ended' ? 'ended' : 'active'}`}>{lifecycle}</span>}
          {confidence && <span className="event-confidence">{confidence}</span>}
          {evidence && <span className="event-evidence-label">{evidence}</span>}
        </span>
      </span>
    </button>
  )
}
