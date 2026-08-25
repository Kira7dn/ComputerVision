import { Activity, Camera, Cpu, Gauge, HardDrive, Thermometer, Wifi } from 'lucide-react'
import { HealthMetricCard } from '@/components/health-metric-card'
import type { MetricsResponse } from '@/types'

interface MetricsGridProps {
  metrics: MetricsResponse | null
  glassLatency: string
}

function value(raw: number | null | undefined, suffix = '') {
  return raw == null ? '--' : `${raw}${suffix}`
}

function metricTone(raw: number | null | undefined, warningAt: number, dangerAt: number): 'healthy' | 'warning' | 'danger' | 'neutral' {
  if (raw == null) return 'neutral'
  if (raw >= dangerAt) return 'danger'
  if (raw >= warningAt) return 'warning'
  return 'healthy'
}

export function MetricsGrid({ metrics, glassLatency }: MetricsGridProps) {
  const memory = metrics?.host.memory
  const gpu = metrics?.gpu
  const pipeline = metrics?.pipeline
  const cameraDetails = pipeline?.camera_details ?? []
  const readyCount = cameraDetails.filter((camera) => Boolean(camera.worker_ready ?? camera.ready)).length
  const cameraCount = cameraDetails.length
  const items = [
    { label: 'Runtime', value: pipeline?.running ? 'Đang chạy' : 'Offline', detail: pipeline?.age_seconds == null ? undefined : `tuổi process ${pipeline.age_seconds}s`, tone: pipeline?.running ? 'healthy' : 'danger', icon: <Activity size={16} /> },
    { label: 'Camera ready', value: `${readyCount}/${cameraCount}`, detail: pipeline?.ready ? 'Tất cả worker sẵn sàng' : 'Cần kiểm tra worker', tone: pipeline?.ready ? 'healthy' : cameraCount ? 'warning' : 'neutral', icon: <Camera size={16} /> },
    { label: 'WSL CPU', value: value(metrics?.host.cpu_percent, '%'), detail: metrics?.host.cpu_cores ? `${metrics.host.cpu_cores} cores` : undefined, tone: metricTone(metrics?.host.cpu_percent, 70, 90), icon: <Cpu size={16} /> },
    { label: 'WSL RAM', value: value(memory?.percent, '%'), detail: memory?.used_mb != null && memory.total_mb != null ? `${memory.used_mb}/${memory.total_mb} MB` : undefined, tone: metricTone(memory?.percent, 75, 90), icon: <HardDrive size={16} /> },
    { label: 'DeepStream', value: value(pipeline?.cpu_percent, '%'), detail: pipeline?.rss_mb == null ? 'CPU process' : `${pipeline.rss_mb} MB RAM`, tone: pipeline?.running ? metricTone(pipeline.cpu_percent, 75, 95) : 'danger', icon: <Gauge size={16} /> },
    { label: 'GPU', value: !gpu?.available ? 'Không khả dụng' : value(gpu.utilization_percent, '%'), detail: gpu?.available ? `${value(gpu.memory_used_mb, ' MB')} · ${value(gpu.temperature_c, '°C')}` : 'Kiểm tra provider', tone: !gpu?.available ? 'warning' : metricTone(gpu.temperature_c, 75, 90), icon: <Thermometer size={16} /> },
    { label: 'Glass-to-glass', value: glassLatency, detail: 'Camera → màn hình · WebRTC', tone: glassLatency === 'offline' ? 'danger' : glassLatency === 'connecting' || glassLatency === 'đang đo' ? 'warning' : 'healthy', icon: <Wifi size={16} /> },
  ] as const

  return (
    <section className="metrics-section header-metrics-section col-span-full min-w-0 pt-0" aria-label="Chỉ số vận hành">
      <div className="section-heading md:hidden"><div><p className="eyebrow">TELEMETRY</p><h2>Chỉ số vận hành</h2></div><span>{metrics ? 'Cập nhật cùng runtime' : 'Đang chờ dữ liệu'}</span></div>
      <div className="metrics-grid grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">{items.map((item) => <HealthMetricCard key={item.label} {...item} />)}</div>
    </section>
  )
}
