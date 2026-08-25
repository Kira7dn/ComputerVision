import type { EventsResponse, MetricsResponse } from '@/types'

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: 'no-store' })
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.json() as Promise<T>
}
export function fetchMetrics(): Promise<MetricsResponse> {
  return getJson<MetricsResponse>('/api/metrics')
}

export function fetchEvents(after: number, limit?: number): Promise<EventsResponse> {
  const params = new URLSearchParams({ after: String(after) })
  if (limit !== undefined) params.set('limit', String(limit))
  return getJson<EventsResponse>(`/api/events?${params.toString()}`)
}
