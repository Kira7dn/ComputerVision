import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type { MetricsResponse } from '@/types'

interface MetricsGridProps {
  metrics: MetricsResponse | null
  browserLatency: string
}
function value(value: number | null | undefined, suffix = '') {
  return value == null ? '--' : `${value}${suffix}`
}

export function MetricsGrid({ metrics, browserLatency }: MetricsGridProps) {
  const memory = metrics?.host.memory
  const gpu = metrics?.gpu
  const pipeline = metrics?.pipeline
  const items = [
    ['WSL CPU', value(metrics?.host.cpu_percent, '%')],
    ['WSL RAM', memory?.percent == null ? '--' : `${memory.percent}% (${memory.used_mb}/${memory.total_mb} MB)`],
    ['DeepStream CPU', !pipeline?.running ? 'stopped' : value(pipeline.cpu_percent, '%')],
    ['DeepStream RAM', !pipeline?.running ? 'stopped' : value(pipeline?.rss_mb, ' MB')],
    ['GPU', !gpu?.available ? 'unavailable' : `${value(gpu?.utilization_percent, '%')} / ${value(gpu?.memory_used_mb, ' MB')} / ${value(gpu?.temperature_c, '°C')}`],
    ['WebRTC status', browserLatency],
  ]

  return (
    <section className="grid grid-cols-2 gap-3 md:grid-cols-3" aria-label="Runtime metrics">
      {items.map(([label, metric]) => (
        <Card key={label} className="bg-card/60">
          <CardHeader className="px-3 pb-1 pt-3">
            <CardTitle className="text-[10px] font-normal uppercase tracking-wide text-muted-foreground">{label}</CardTitle>
          </CardHeader>
          <CardContent className="px-3 pb-3 text-sm font-medium tabular-nums">{metric}</CardContent>
        </Card>
      ))}
    </section>
  )
}
