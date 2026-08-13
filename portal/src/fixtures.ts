// Shared test fixtures matching the shapes pinned in
// backend/src/portal/schemas.py.
import type { FeedItem, GateSetting, MetricsResponse } from './api'

export function pendingFeedItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    run_id: 1,
    ticket_id: 'T-100',
    route: 'status',
    confidence: 0.92,
    outcome: null,
    draft_id: 1,
    draft_status: 'pending',
    draft_body: 'Original draft body.',
    edited_body: null,
    sent_body: null,
    escalation_reason: null,
    trace_url: 'https://cloud.langfuse.com/trace/abc123',
    received_at: '2026-08-13T12:00:00Z',
    replied_at: null,
    ...overrides,
  }
}

export function emptyGate(enabled = false): GateSetting {
  return { enabled }
}

export function emptyMetrics(overrides: Partial<MetricsResponse> = {}): MetricsResponse {
  return {
    human_avoidance_rate: 0,
    latency_p50_s: 0,
    latency_p95_s: 0,
    escalations_by_reason: {},
    ...overrides,
  }
}
