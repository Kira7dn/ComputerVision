import type { EventRecord } from '@/types'
import { eventLabel, formatEventTime } from '@/components/event-list-item'

interface EventDetailMetadataProps {
  event: EventRecord
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '--'
  if (typeof value === 'boolean') return value ? 'Có' : 'Không'
  return String(value)
}

export function EventDetailMetadata({ event }: EventDetailMetadataProps) {
  const confidence = event.confidence == null ? null : `${(Number(event.confidence) * 100).toFixed(2)}%`
  const metadata: Array<[string, unknown]> = [
    ['Camera', event.camera],
    ['Thời gian', formatEventTime(event.timestamp)],
    ['Sự kiện', eventLabel(event)],
    ['Phân loại', event.classification],
    ['Độ tin cậy', confidence],
    ['Trạng thái xác nhận', event.confirmation_state],
    ['Region track', event.region_track_id],
    ['Person track', event.person_track_id],
    ['Best frame', event.best_frame_number],
    ['Detector hits', event.detector_hits],
    ['Notification', event.notification_emitted],
  ]

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
      <details className="event-raw-details">
        <summary>Thông tin JSON raw</summary>
        <pre>{JSON.stringify({ event, details: event.details ?? {}, start_record: event.start_record ?? {} }, null, 2)}</pre>
      </details>
    </div>
  )
}
