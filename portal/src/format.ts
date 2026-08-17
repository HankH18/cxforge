// Small display-formatting helpers shared by the feed, draft detail, and
// metrics panel. No business logic lives here — just presentation.

import type { DraftStatus, RunOutcome } from './api-types'

// Two different facts, and the portal must not confuse them:
//
//   `run.outcome`  — what the RUN did. Authoritative.
//   `draft.status` — what happened to the reply TEXT.
//
// `act` records every draft it sends with status "auto_sent"
// (backend/src/agent/nodes.py, the `store.record_draft(..., status="auto_sent")` call at
// the end of `act` — cited by symbol, not line, because that file moves) — escalations
// and off-topic replies
// included, where the sent text is only an acknowledgement and a human
// still owns the ticket. Labelling a row from draft status alone reports an
// escalation as an autonomous resolution.
const OUTCOME_LABELS: Record<RunOutcome, string> = {
  auto_sent: 'auto-sent',
  gated_sent: 'sent after review',
  rejected: 'rejected',
  escalated: 'escalated',
  off_topic: 'off-topic',
}

const DRAFT_LABELS: Record<DraftStatus, string> = {
  pending: 'pending review',
  approved: 'approved',
  rejected: 'rejected',
  auto_sent: 'sent, no review',
}

/** `outcome` is NULL while the gate holds a draft: the run has not reached
 * a terminal state yet, which is worth naming rather than blanking. */
export function formatOutcome(outcome: RunOutcome | null | undefined): string {
  return outcome === null || outcome === undefined ? 'awaiting review' : OUTCOME_LABELS[outcome]
}

/** Class-hook suffix for an outcome — never rendered as text. */
export function outcomeSlug(outcome: RunOutcome | null | undefined): string {
  return outcome === null || outcome === undefined ? 'awaiting' : outcome.replace(/_/g, '-')
}

export function formatDraftStatus(status: DraftStatus | null | undefined): string {
  return status === null || status === undefined ? 'no draft' : DRAFT_LABELS[status]
}

/** Menu labels for the draft-status filter, in display order. */
export const DRAFT_STATUS_LABELS: Readonly<Record<DraftStatus, string>> = DRAFT_LABELS

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(0)}%`
}

export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value.toFixed(1)}s`
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

/** Human-readable message for anything thrown by the API client — used so
 * the reviewer sees a real explanation instead of "[object Object]". */
export function describeError(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
