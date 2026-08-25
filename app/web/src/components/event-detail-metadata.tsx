import type { EventRecord } from '@/types'
import { dmsAlertLabels, dmsMetrics, dmsStatusLabel, eventLabel, eventLifecycleLabel, eventSeverityLabel, formatEventScore, formatEventTime } from '@/components/event-list-item'

interface EventDetailMetadataProps {
  event: EventRecord
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '--'
  if (typeof value === 'boolean') return value ? 'Có' : 'Không'
  return String(value)
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

export function EventDetailMetadata({ event }: EventDetailMetadataProps) {
  const dmsAlerts = dmsAlertLabels(event)
  const metrics = dmsMetrics(event)
  const details = asRecord(event.details)
  const dmsEvidence = asRecord(details.dms_evidence)
  const confidence = formatEventScore(event, 2)
  const metadata: Array<[string, unknown]> = [
    ['Camera', event.camera],
    ['Thời gian', formatEventTime(event.timestamp)],
    ['Vòng đời', eventLifecycleLabel(event)],
    ['Bắt đầu', formatEventTime(event.started_at ?? event.timestamp)],
    ['Cập nhật cuối', formatEventTime(event.updated_at ?? event.timestamp)],
    ['Kết thúc', event.ended_at == null ? null : formatEventTime(event.ended_at)],
    ['Sự kiện', eventLabel(event)],
    ['Phân loại', event.classification],
    ['Mức độ', eventSeverityLabel(event)],
    ['Độ tin cậy', confidence],
    ['Trạng thái xác nhận', event.confirmation_state],
    ['Region track', event.region_track_id],
    ['Person track', event.person_track_id],
    ['Best frame', event.best_frame_number],
    ['Detector hits', event.detector_hits],
    ['Notification', event.notification_emitted],
  ]
  if (event.function === 'dms') {
    metadata.splice(4, 0, ['Cảnh báo DMS', dmsAlerts[0] ?? null])
    metadata.splice(5, 0, ['Trạng thái DMS', dmsStatusLabel(event)])
    metadata.splice(6, 0, ['Loại bằng chứng', dmsEvidence.type])
    metadata.splice(7, 0, [
      'Xác nhận',
      typeof dmsEvidence.confirmation_hits === 'number' && typeof dmsEvidence.required_hits === 'number'
        ? `${dmsEvidence.confirmation_hits}/${dmsEvidence.required_hits} khung`
        : null,
    ])
    metadata.splice(8, 0, ['Lý do kết thúc', details.end_reason])
  }

  return (
    <div className="space-y-4">
      <div className="event-detail-grid">
        {metadata.map(([label, value]) => <div className="event-detail-field" key={label}><span>{label}</span><strong>{display(value)}</strong></div>)}
      </div>
      {(event.best_bbox || event.best_person_bbox || event.best_model_roi_bbox) && (
        <div className="event-detail-grid">
          {event.best_bbox && <div className="event-detail-field"><span>Region bbox</span><strong>{event.best_bbox.join(', ')}</strong></div>}
          {event.best_person_bbox && <div className="event-detail-field"><span>Person bbox</span><strong>{event.best_person_bbox.join(', ')}</strong></div>}
          {event.best_model_roi_bbox && <div className="event-detail-field"><span>Model ROI</span><strong>{event.best_model_roi_bbox.join(', ')}</strong></div>}
        </div>
      )}
      {event.function === 'dms' && Object.keys(metrics).length > 0 && (
        <div className="event-detail-grid">
          <div className="event-detail-field"><span>Face detected</span><strong>{display(metrics.face_detected)}</strong></div>
          <div className="event-detail-field"><span>EAR</span><strong>{display(metrics.ear)}</strong></div>
          <div className="event-detail-field"><span>MAR</span><strong>{display(metrics.mar)}</strong></div>
          <div className="event-detail-field"><span>Yaw / Pitch</span><strong>{display(metrics.yaw_deg)}° / {display(metrics.pitch_deg)}°</strong></div>
          <div className="event-detail-field"><span>Face latency</span><strong>{display(metrics.face_latency_ms)} ms</strong></div>
          <div className="event-detail-field"><span>Total latency</span><strong>{display(metrics.total_latency_ms)} ms</strong></div>
          <div className="event-detail-field"><span>Evidence bbox</span><strong>{display(Array.isArray(dmsEvidence.best_bbox) ? dmsEvidence.best_bbox.join(', ') : null)}</strong></div>
          <div className="event-detail-field"><span>Person track</span><strong>{display(dmsEvidence.person_track_id)}</strong></div>
        </div>
      )}
      <details className="event-raw-details">
        <summary>Thông tin JSON raw</summary>
        <pre>{JSON.stringify({ event, details: event.details ?? {}, start_record: event.start_record ?? {} }, null, 2)}</pre>
      </details>
    </div>
  )
}
