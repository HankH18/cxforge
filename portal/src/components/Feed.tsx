import type { DraftStatus, FeedItem } from '../api'
import {
  DRAFT_STATUS_LABELS,
  formatDraftStatus,
  formatOutcome,
  formatPercent,
  formatTimestamp,
  outcomeSlug,
} from '../format'

interface FeedProps {
  runs: FeedItem[]
  error: string | null
  loading: boolean
  statusFilter: DraftStatus | ''
  onStatusFilterChange: (status: DraftStatus | '') => void
  selectedDraftId: number | null
  onSelect: (item: FeedItem) => void
}

const STATUS_OPTIONS: Array<DraftStatus | ''> = ['', 'pending', 'approved', 'rejected', 'auto_sent']

// The Outcome column is driven by `run.outcome` and the Draft column by
// `draft.status`; `format.ts` documents why conflating the two misreports
// every escalation as an autonomous resolution.
//
// R10: every agent run, showing route/confidence/outcome/escalation
// reason/trace link, with pending drafts visibly distinct from sent runs.
export default function Feed({
  runs,
  error,
  loading,
  statusFilter,
  onStatusFilterChange,
  selectedDraftId,
  onSelect,
}: FeedProps) {
  return (
    <section className="panel feed" aria-label="Feed">
      <header className="panel__header">
        <h2 className="panel__title">Run feed</h2>
        <label className="feed__filter">
          <span className="feed__filter-label">Filter by draft status</span>{' '}
          <select
            className="feed__filter-select"
            value={statusFilter}
            onChange={(event) => onStatusFilterChange(event.target.value as DraftStatus | '')}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option || 'all'} value={option}>
                {option === '' ? 'all drafts' : DRAFT_STATUS_LABELS[option]}
              </option>
            ))}
          </select>
        </label>
      </header>

      {error && (
        <p className="alert alert--error" role="alert">
          Could not load feed: {error}
        </p>
      )}

      {runs.length === 0 && !error ? (
        loading ? (
          <p className="feed__loading placeholder">Loading the run feed…</p>
        ) : (
          <div className="feed__empty placeholder">
            <p className="placeholder__headline">No runs yet.</p>
            <p className="placeholder__detail">
              Every ticket the agent handles lands here within seconds of arriving — route,
              confidence, what it decided, and the reply it wrote.
            </p>
          </div>
        )
      ) : (
        <div className="table-scroll">
          <table className="feed__table">
            <thead>
              <tr>
                <th scope="col">Ticket</th>
                <th scope="col">Route</th>
                <th scope="col">Confidence</th>
                <th scope="col">Outcome</th>
                <th scope="col">Draft</th>
                <th scope="col">Escalation reason</th>
                <th scope="col">Received</th>
                <th scope="col">Trace</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {runs.map((item) => {
                const isPending = item.draft_status === 'pending'
                const isSelected = item.draft_id !== null && item.draft_id === selectedDraftId
                return (
                  <tr
                    key={item.run_id}
                    className={`feed__row${isSelected ? ' feed__row--selected' : ''}${
                      isPending ? ' feed__row--pending' : ''
                    }`}
                    aria-current={isSelected ? 'true' : undefined}
                  >
                    <td className="feed__cell feed__cell--ticket">{item.ticket_id}</td>
                    <td className="feed__cell feed__cell--route">{item.route ?? '—'}</td>
                    <td className="feed__cell feed__cell--confidence">
                      {formatPercent(item.confidence)}
                    </td>
                    <td className="feed__cell feed__cell--outcome">
                      <span className={`badge badge--outcome-${outcomeSlug(item.outcome)}`}>
                        {formatOutcome(item.outcome)}
                      </span>
                    </td>
                    <td className="feed__cell feed__cell--draft">
                      <span className={`badge badge--draft-${item.draft_status ?? 'none'}`}>
                        {formatDraftStatus(item.draft_status)}
                      </span>
                    </td>
                    <td className="feed__cell feed__cell--reason">{item.escalation_reason ?? '—'}</td>
                    <td className="feed__cell feed__cell--received">
                      {formatTimestamp(item.received_at)}
                    </td>
                    <td className="feed__cell feed__cell--trace">
                      {item.trace_url ? (
                        <a
                          className="feed__trace-link"
                          href={item.trace_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          trace
                        </a>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="feed__cell feed__cell--actions">
                      {item.draft_id !== null && (
                        <button
                          type="button"
                          className={`button ${isPending ? 'button--primary' : 'button--ghost'}`}
                          onClick={() => onSelect(item)}
                        >
                          {isPending ? 'Review' : 'View'}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
