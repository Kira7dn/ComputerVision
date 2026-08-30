import type { EventsResponse, MetricsResponse } from '@/types'

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: 'no-store' })
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.json() as Promise<T>
}
export function fetchMetrics(): Promise<MetricsResponse> {
  return getJson<MetricsResponse>('/api/metrics')
}

export function fetchEvents(): Promise<EventsResponse> {
  return getJson<EventsResponse>('/api/events')
}
