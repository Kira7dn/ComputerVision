import type { ReactNode } from 'react'
import { CheckCircle2, CircleAlert, CircleDashed } from 'lucide-react'

interface HealthMetricCardProps {
  label: string
  value: string
  tone?: 'healthy' | 'warning' | 'danger' | 'neutral'
  icon: ReactNode
  detail?: string
}

export function HealthMetricCard({ label, value, tone = 'neutral', icon, detail }: HealthMetricCardProps) {
  const StatusIcon = tone === 'healthy' ? CheckCircle2 : tone === 'danger' ? CircleAlert : tone === 'warning' ? CircleDashed : null
  return (
    <article className={`health-metric-card health-metric-card-${tone}`}>
      <div className="health-metric-topline"><span className="health-metric-icon">{icon}</span><span className="health-metric-label">{label}</span>{StatusIcon && <StatusIcon className="health-metric-status" size={14} aria-hidden="true" />}</div>
      <strong className="health-metric-value">{value}</strong>
      {detail && <span className="health-metric-detail">{detail}</span>}
    </article>
  )
}
