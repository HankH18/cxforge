// Portal API client — request/response types are GENERATED from
// backend/src/portal/schemas.py via backend/src/portal/codegen.py (T-19).
// See portal/src/api-types.ts (generated, do not edit) for the source; this
// file keeps only the hand-written fetch logic below. Every request carries
// the X-Portal-Token shared secret; the token comes from build-time env
// (VITE_PORTAL_TOKEN), never a literal in source.

import type { DraftResponse, DraftStatus, FeedResponse, GateSetting, MetricsResponse } from './api-types'

export type {
  DraftEditRequest,
  DraftResponse,
  DraftStatus,
  FeedItem,
  FeedResponse,
  GateSetting,
  MetricsResponse,
  RunOutcome,
} from './api-types'

const PORTAL_TOKEN: string = import.meta.env.VITE_PORTAL_TOKEN ?? ''

// Thrown for any non-2xx response. Callers surface `.message` (the
// backend's `detail`, when present) to the reviewer instead of pretending
// the operation succeeded — routes.py returns 404/409/502 for the
// not-found / not-pending / downstream-send-failed cases and the UI must
// not swallow any of them.
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Portal-Token': PORTAL_TOKEN,
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let detail = response.statusText || `request failed with status ${response.status}`
    try {
      const body: unknown = await response.json()
      if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
        detail = body.detail
      }
    } catch {
      // Non-JSON error body — fall back to statusText above.
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export function fetchFeed(status?: DraftStatus): Promise<FeedResponse> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return request<FeedResponse>(`/api/feed${qs}`)
}

export function editDraft(draftId: number, body: string): Promise<DraftResponse> {
  return request<DraftResponse>(`/api/drafts/${draftId}`, {
    method: 'PUT',
    body: JSON.stringify({ body }),
  })
}

export function approveDraft(draftId: number): Promise<DraftResponse> {
  return request<DraftResponse>(`/api/drafts/${draftId}/approve`, { method: 'POST' })
}

export function rejectDraft(draftId: number): Promise<DraftResponse> {
  return request<DraftResponse>(`/api/drafts/${draftId}/reject`, { method: 'POST' })
}

export function getGate(): Promise<GateSetting> {
  return request<GateSetting>('/api/settings/gate')
}

export function setGate(enabled: boolean): Promise<GateSetting> {
  return request<GateSetting>('/api/settings/gate', {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  })
}

export function getMetrics(): Promise<MetricsResponse> {
  return request<MetricsResponse>('/api/metrics')
}
