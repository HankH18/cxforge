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
      <section aria-label="Metrics">
        <h2>Metrics</h2>
        <p role="alert">Could not load metrics: {error}</p>
      </section>
    )
  }

  if (!metrics) {
    return (
      <section aria-label="Metrics">
        <h2>Metrics</h2>
        <p>Loading metrics…</p>
      </section>
    )
  }

  const reasonEntries = Object.entries(metrics.escalations_by_reason)

  return (
    <section aria-label="Metrics">
      <h2>Metrics</h2>
      <dl>
        <dt>Human-avoidance rate</dt>
        <dd>{formatPercent(metrics.human_avoidance_rate)}</dd>
        <dt>Latency p50</dt>
        <dd>{formatSeconds(metrics.latency_p50_s)}</dd>
        <dt>Latency p95</dt>
        <dd>{formatSeconds(metrics.latency_p95_s)}</dd>
      </dl>
      <h3>Escalations by reason</h3>
      {reasonEntries.length === 0 ? (
        <p>No escalations recorded.</p>
      ) : (
        <>
          <ul>
            {reasonEntries.map(([reason, count]) => (
              <li key={reason}>
                {reason}: {count}
              </li>
            ))}
          </ul>
          <p>
            A run escalated for more than one reason is counted under every reason it triggered,
            so these counts can add up to more than the number of escalated runs — they are not a
            breakdown that sums to a total.
          </p>
        </>
      )}
    </section>
  )
}
