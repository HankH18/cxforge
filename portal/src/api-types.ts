// GENERATED FILE — DO NOT EDIT BY HAND.
// Source of truth: backend/src/portal/schemas.py, via FastAPI's OpenAPI
// schema (`app.openapi()`).
// Regenerate:
//   uv run python backend/src/portal/codegen.py --out portal/src/api-types.ts
// Verify (no write): same command with --check

export type DraftStatus = 'pending' | 'approved' | 'rejected' | 'auto_sent'

export type RunOutcome = 'auto_sent' | 'gated_sent' | 'rejected' | 'escalated' | 'off_topic'

export interface DraftEditRequest {
  body: string
}

export interface DraftResponse {
  draft_id: number
  run_id: number
  ticket_id: string
  status: DraftStatus
  body: string
  edited_body: string | null
  sent_body?: string | null
}

export interface FeedItem {
  run_id: number
  ticket_id: string
  route: string | null
  confidence: number | null
  outcome: RunOutcome | null
  draft_id: number | null
  draft_status: DraftStatus | null
  draft_body: string | null
  edited_body: string | null
  sent_body: string | null
  escalation_reason: string | null
  trace_url: string | null
  received_at: string | null
  replied_at: string | null
}

export interface FeedResponse {
  runs: FeedItem[]
}

export interface GateSetting {
  enabled: boolean
}

export interface MetricsResponse {
  human_avoidance_rate: number
  latency_p50_s: number
  latency_p95_s: number
  escalations_by_reason: Record<string, number>
}
