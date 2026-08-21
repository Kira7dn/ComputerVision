import { Activity, Clock3, RefreshCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { MetricsGrid } from '@/components/metrics-grid'
import type { MetricsResponse } from '@/types'

interface DashboardHeaderProps {
  metrics: MetricsResponse | null
  status: string
  apiError: boolean
  browserLatency: string
}

function formatUpdatedAt(timestamp?: number) {
  if (!timestamp) return 'Chưa có dữ liệu'
  return new Date(timestamp * 1000).toLocaleTimeString('vi-VN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function runtimeTone(metrics: MetricsResponse | null, apiError: boolean) {
  if (apiError) return 'warning'
  if (!metrics?.pipeline.running) return 'danger'
  if (metrics.pipeline.ready) return 'healthy'
  return 'warning'
}

function StatusDot({ tone }: { tone: 'healthy' | 'warning' | 'danger' | 'neutral' }) {
  return <span className={`status-dot status-dot-${tone}`} aria-hidden="true" />
}

export function DashboardHeader({ metrics, status, apiError, browserLatency }: DashboardHeaderProps) {
  const tone = runtimeTone(metrics, apiError)
  const statusLabel = apiError ? 'Đang kết nối lại' : metrics?.pipeline.running ? status : 'Runtime offline'

  return (
    <header className="dashboard-header flex min-w-0 flex-col gap-4 py-1 md:flex-row md:items-start md:gap-8">
      <div className="header-brand min-w-0 shrink-0">
        <div className="flex items-center gap-3">
          <div className="brand-mark" aria-hidden="true"><Activity size={18} strokeWidth={2.5} /></div>
          <div className="min-w-0">
            <p className="eyebrow">LS-VISION / CONTROL ROOM</p>
            <h1 className="truncate text-xl font-semibold tracking-tight sm:text-2xl">Giám sát trực tiếp</h1>
          </div>
        </div>
        <div className="header-runtime-meta mt-2 flex flex-wrap items-center gap-2">
          <Badge className={`status-badge status-badge-${tone}`} variant="outline">
            <StatusDot tone={tone} />
            {statusLabel}
          </Badge>
          <span className="header-updated"><Clock3 size={13} /> {formatUpdatedAt(metrics?.timestamp)}</span>
          {apiError && <span className="summary-warning" role="status"><RefreshCw size={13} /> Dữ liệu cũ</span>}
        </div>
      </div>

      <div className="header-metrics-wrap min-w-0 flex-1">
        <MetricsGrid metrics={metrics} browserLatency={browserLatency} />
      </div>
    </header>
  )
}
