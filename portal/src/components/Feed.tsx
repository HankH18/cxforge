import type { DraftStatus, FeedItem } from '../api'
import { formatPercent, formatTimestamp } from '../format'

interface FeedProps {
  runs: FeedItem[]
  error: string | null
  statusFilter: DraftStatus | ''
  onStatusFilterChange: (status: DraftStatus | '') => void
  selectedDraftId: number | null
  onSelect: (item: FeedItem) => void
}

const STATUS_OPTIONS: Array<DraftStatus | ''> = ['', 'pending', 'approved', 'rejected', 'auto_sent']

// R10: every agent run, showing route/confidence/escalation reason/trace
// link, with pending drafts visibly distinct from already-sent runs.
export default function Feed({
  runs,
  error,
  statusFilter,
  onStatusFilterChange,
  selectedDraftId,
  onSelect,
}: FeedProps) {
  return (
    <section aria-label="Feed">
      <h2>Feed</h2>
      <label>
        Filter by draft status{' '}
        <select
          value={statusFilter}
          onChange={(event) => onStatusFilterChange(event.target.value as DraftStatus | '')}
        >
          {STATUS_OPTIONS.map((option) => (
            <option key={option || 'all'} value={option}>
              {option === '' ? 'all' : option}
            </option>
          ))}
        </select>
      </label>
      {error && <p role="alert">Could not load feed: {error}</p>}
      {runs.length === 0 && !error ? (
        <p>No runs yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th scope="col">Ticket</th>
              <th scope="col">Route</th>
              <th scope="col">Confidence</th>
              <th scope="col">Status</th>
              <th scope="col">Escalation reason</th>
              <th scope="col">Received</th>
              <th scope="col">Trace</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {runs.map((item) => {
              const isPending = item.draft_status === 'pending'
              const isSent = item.draft_status === 'approved' || item.draft_status === 'auto_sent'
              return (
                <tr
                  key={item.run_id}
                  aria-current={item.draft_id === selectedDraftId ? 'true' : undefined}
                >
                  <td>{item.ticket_id}</td>
                  <td>{item.route ?? '—'}</td>
                  <td>{formatPercent(item.confidence)}</td>
                  <td>
                    {isPending && 'pending review'}
                    {isSent && (item.draft_status === 'auto_sent' ? 'auto-sent' : 'sent (approved)')}
                    {item.draft_status === 'rejected' && 'rejected'}
                    {!item.draft_status && 'no draft'}
                  </td>
                  <td>{item.escalation_reason ?? '—'}</td>
                  <td>{formatTimestamp(item.received_at)}</td>
                  <td>
                    {item.trace_url ? (
                      <a href={item.trace_url} target="_blank" rel="noreferrer">
                        trace
                      </a>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    {item.draft_id !== null && (
                      <button type="button" onClick={() => onSelect(item)}>
                        {isPending ? 'Review' : 'View'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
