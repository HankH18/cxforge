import { useEffect, useState } from 'react'

import { approveDraft, editDraft, rejectDraft, type DraftResponse, type FeedItem } from '../api'
import { describeError, formatPercent } from '../format'

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
    <section aria-label="Draft detail">
      <h2>Ticket {item.ticket_id}</h2>
      <button type="button" onClick={onClose}>
        Close
      </button>
      <dl>
        <dt>Route</dt>
        <dd>{item.route ?? '—'}</dd>
        <dt>Confidence</dt>
        <dd>{formatPercent(item.confidence)}</dd>
        <dt>Escalation reason</dt>
        <dd>{item.escalation_reason ?? '—'}</dd>
        <dt>Draft status</dt>
        <dd>{item.draft_status ?? 'no draft'}</dd>
      </dl>

      {item.draft_id === null && <p>No draft exists for this run.</p>}

      {item.draft_id !== null && isPending && (
        <>
          <label htmlFor="draft-body">
            Reply body — this exact text is what gets sent when you approve
          </label>
          <textarea
            id="draft-body"
            value={editedText}
            onChange={(event) => setEditedText(event.target.value)}
            disabled={busy}
            rows={8}
            cols={60}
          />
          {isDirty && <p>Edited from the original draft — the text above will be sent, not the original.</p>}
          <div>
            <button type="button" onClick={handleApprove} disabled={busy}>
              Approve{isDirty ? ' edited reply' : ''}
            </button>
            <button type="button" onClick={handleReject} disabled={busy}>
              Reject
            </button>
          </div>
        </>
      )}

      {item.draft_id !== null && !isPending && (
        <>
          <h3>
            {item.draft_status === 'rejected' ? 'Rejected — nothing was sent' : 'Sent'}
          </h3>
          <pre>{item.sent_body ?? item.draft_body ?? '(no body)'}</pre>
        </>
      )}

      {error && <p role="alert">Action failed: {error}</p>}
    </section>
  )
}
