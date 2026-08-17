import type { MetricsResponse } from '../api'
import { formatPercent, formatSeconds } from '../format'

interface MetricsPanelProps {
  metrics: MetricsResponse | null
  error: string | null
}

// R13: human-avoidance rate, p50/p95 latency, escalation counts by reason.
export default function MetricsPanel({ metrics, error }: MetricsPanelProps) {
  if (error) {
    return (
      <section className="panel metrics" aria-label="Metrics">
        <header className="panel__header">
          <h2 className="panel__title">Metrics</h2>
        </header>
        <p className="alert alert--error" role="alert">
          Could not load metrics: {error}
        </p>
      </section>
    )
  }

  if (!metrics) {
    return (
      <section className="panel metrics" aria-label="Metrics">
        <header className="panel__header">
          <h2 className="panel__title">Metrics</h2>
        </header>
        <p className="metrics__loading placeholder">Reading the run history…</p>
      </section>
    )
  }

  const reasonEntries = Object.entries(metrics.escalations_by_reason)

  return (
    <section className="panel metrics" aria-label="Metrics">
      <header className="panel__header">
        <h2 className="panel__title">Metrics</h2>
      </header>

      <dl className="metrics__stats">
        <div className="metrics__stat">
          <dt className="metrics__label">Human-avoidance rate</dt>
          <dd className="metrics__value">{formatPercent(metrics.human_avoidance_rate)}</dd>
        </div>
        <div className="metrics__stat">
          <dt className="metrics__label">Latency p50</dt>
          <dd className="metrics__value">{formatSeconds(metrics.latency_p50_s)}</dd>
        </div>
        <div className="metrics__stat">
          <dt className="metrics__label">Latency p95</dt>
          <dd className="metrics__value">{formatSeconds(metrics.latency_p95_s)}</dd>
        </div>
      </dl>

      <h3 className="metrics__subtitle">Escalations by reason</h3>
      {reasonEntries.length === 0 ? (
        <p className="metrics__empty placeholder">
          No escalations yet — every run so far resolved without handing the ticket to a human.
        </p>
      ) : (
        <>
          <ul className="metrics__reasons">
            {reasonEntries.map(([reason, count]) => (
              <li className="metrics__reason" key={reason}>
                <span className="metrics__reason-name">{reason}</span>
                <span className="metrics__reason-count">{count}</span>
              </li>
            ))}
          </ul>
          <p className="note">
            A run escalated for more than one reason is counted under every reason it triggered,
            so these counts can add up to more than the number of escalated runs — they are not a
            breakdown that sums to a total.
          </p>
        </>
      )}
    </section>
  )
}
