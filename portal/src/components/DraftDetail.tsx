import { useEffect, useState } from 'react'

import { approveDraft, editDraft, rejectDraft, type DraftResponse, type FeedItem } from '../api'
import {
  describeError,
  formatDraftStatus,
  formatOutcome,
  formatPercent,
  outcomeSlug,
} from '../format'

interface DraftDetailProps {
  item: FeedItem
  onClose: () => void
  onChanged: (result: DraftResponse) => void
}

// R11: editable body, approve, reject. Editing then approving must send the
// EDITED text — the backend's approve endpoint sends whatever is currently
// persisted as `edited_body` (falling back to the original `body`), so this
// component persists the on-screen text via PUT /api/drafts/{id} BEFORE
// calling POST .../approve whenever the reviewer has changed it. That
// ordering is the entire point of this component: skip the PUT and an edit
// silently never reaches the customer.
export default function DraftDetail({ item, onClose, onChanged }: DraftDetailProps) {
  const originalBody = item.edited_body ?? item.draft_body ?? ''
  const [editedText, setEditedText] = useState(originalBody)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A newly-selected draft (or a refreshed one from polling) resets the
  // editor to its current persisted text.
  useEffect(() => {
    setEditedText(originalBody)
    setError(null)
  }, [item.draft_id, originalBody])

  const isPending = item.draft_status === 'pending'
  const isDirty = editedText !== originalBody

  async function handleApprove() {
    if (item.draft_id === null || busy) return
    setBusy(true)
    setError(null)
    try {
      if (isDirty) {
        await editDraft(item.draft_id, editedText)
      }
      const result = await approveDraft(item.draft_id)
      onChanged(result)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  async function handleReject() {
    if (item.draft_id === null || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await rejectDraft(item.draft_id)
      onChanged(result)
    } catch (err) {
      setError(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel draft" aria-label="Draft detail">
      <header className="panel__header">
        <h2 className="panel__title">Ticket {item.ticket_id}</h2>
        <button type="button" className="button button--ghost draft__close" onClick={onClose}>
          Close
        </button>
      </header>

      <dl className="draft__meta">
        <div className="draft__meta-item">
          <dt className="draft__meta-label">Route</dt>
          <dd className="draft__meta-value">{item.route ?? '—'}</dd>
        </div>
        <div className="draft__meta-item">
          <dt className="draft__meta-label">Confidence</dt>
          <dd className="draft__meta-value">{formatPercent(item.confidence)}</dd>
        </div>
        <div className="draft__meta-item">
          <dt className="draft__meta-label">Run outcome</dt>
          <dd className="draft__meta-value">
            <span className={`badge badge--outcome-${outcomeSlug(item.outcome)}`}>
              {formatOutcome(item.outcome)}
            </span>
          </dd>
        </div>
        <div className="draft__meta-item">
          <dt className="draft__meta-label">Escalation reason</dt>
          <dd className="draft__meta-value">{item.escalation_reason ?? '—'}</dd>
        </div>
        <div className="draft__meta-item">
          <dt className="draft__meta-label">Draft status</dt>
          <dd className="draft__meta-value">
            <span className={`badge badge--draft-${item.draft_status ?? 'none'}`}>
              {formatDraftStatus(item.draft_status)}
            </span>
          </dd>
        </div>
      </dl>

      {item.draft_id === null && (
        <div className="draft__empty placeholder">
          <p className="placeholder__headline">No draft exists for this run.</p>
          <p className="placeholder__detail">
            The agent reached an outcome without writing a reply for review — the run row
            above is the whole record.
          </p>
        </div>
      )}

      {item.draft_id !== null && isPending && (
        <div className="draft__editor">
          <label className="draft__editor-label" htmlFor="draft-body">
            Reply body — this exact text is what gets sent when you approve
          </label>
          <textarea
            className="draft__textarea"
            id="draft-body"
            value={editedText}
            onChange={(event) => setEditedText(event.target.value)}
            disabled={busy}
            rows={8}
          />
          {isDirty && (
            <p className="draft__dirty note">
              Edited from the original draft — the text above will be sent, not the original.
            </p>
          )}
          <div className="draft__actions">
            <button
              type="button"
              className="button button--primary"
              onClick={handleApprove}
              disabled={busy}
            >
              Approve{isDirty ? ' edited reply' : ''}
            </button>
            <button
              type="button"
              className="button button--danger"
              onClick={handleReject}
              disabled={busy}
            >
              Reject
            </button>
          </div>
          {busy && <p className="draft__busy note">Working…</p>}
        </div>
      )}

      {item.draft_id !== null && !isPending && (
        <div className="draft__sent">
          <h3 className="draft__sent-title">
            {item.draft_status === 'rejected' ? 'Rejected — nothing was sent' : 'Sent'}
          </h3>
          <pre className="draft__body">{item.sent_body ?? item.draft_body ?? '(no body)'}</pre>
        </div>
      )}

      {error && (
        <p className="alert alert--error" role="alert">
          Action failed: {error}
        </p>
      )}
    </section>
  )
}
