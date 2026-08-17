// W2-C3 — the metrics panel must not present an unmeasured latency as a fast one.
//
// `/api/metrics` used to report `latency_p50_s = latency_p95_s = 0.0` on an empty run
// history, and the panel rendered that as "0.0s". SPEC success criterion 6 is
// "p95 < 5 min", so a freshly deployed stack that had never run anything displayed the
// best possible evidence for a claim nobody had tested — `docs/STATE.md §4.1`.
//
// The backend now sends `null` and a `sample_count`. These tests are the front half of
// that contract: that the panel renders the null honestly, and that a reader can always
// see how many runs the numbers rest on. Asserting the em dash alone would not be
// enough — an em dash also means "loading" or "broken" — so the run count and the
// explanatory line are asserted too.

import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import MetricsPanel from './components/MetricsPanel'
import { emptyMetrics } from './fixtures'

/** The <dd> for a given metric label, via the <div> that pairs them. */
function valueFor(label: string): HTMLElement {
  const term = screen.getByText(label)
  const pair = term.closest('.metrics__stat')
  if (!pair) throw new Error(`no .metrics__stat wrapper for ${label}`)
  const value = pair.querySelector('.metrics__value')
  if (!value) throw new Error(`no .metrics__value inside the pair for ${label}`)
  return value as HTMLElement
}

describe('an unmeasured latency', () => {
  it('renders as a dash and never as a number', () => {
    render(<MetricsPanel metrics={emptyMetrics()} error={null} />)

    expect(valueFor('Latency p50').textContent).toBe('—')
    expect(valueFor('Latency p95').textContent).toBe('—')
    // The specific regression: the old panel showed "0.0s" here, which reads
    // as a pass against "p95 < 5 min".
    expect(valueFor('Latency p95').textContent).not.toMatch(/\d/)
  })

  it('says why it is blank, so a dash is not read as a broken panel', () => {
    render(<MetricsPanel metrics={emptyMetrics()} error={null} />)

    expect(screen.getByText(/no latency to report/i)).toBeTruthy()
    expect(screen.getByText(/an unmeasured p95 is not a fast one/i)).toBeTruthy()
  })

  it('shows the run count the percentiles rest on', () => {
    render(<MetricsPanel metrics={emptyMetrics()} error={null} />)

    expect(valueFor('Runs measured').textContent).toBe('0')
  })
})

describe('a measured latency', () => {
  const measured = emptyMetrics({
    human_avoidance_rate: 0.625,
    latency_p50_s: 30,
    latency_p95_s: 48,
    sample_count: 5,
  })

  it('shows the numbers and the size of the sample behind them', () => {
    render(<MetricsPanel metrics={measured} error={null} />)

    expect(valueFor('Latency p50').textContent).toBe('30.0s')
    expect(valueFor('Latency p95').textContent).toBe('48.0s')
    expect(valueFor('Runs measured').textContent).toBe('5')
    expect(screen.getByText(/computed over the 5 autonomously sent replies/i)).toBeTruthy()
  })

  it('drops the empty-state explanation once there is something to explain', () => {
    render(<MetricsPanel metrics={measured} error={null} />)

    expect(screen.queryByText(/no latency to report/i)).toBeNull()
  })

  it('says "reply" for a single run rather than "1 replies"', () => {
    render(
      <MetricsPanel
        metrics={emptyMetrics({ latency_p50_s: 12, latency_p95_s: 12, sample_count: 1 })}
        error={null}
      />,
    )

    expect(screen.getByText(/computed over the 1 autonomously sent reply/i)).toBeTruthy()
  })
})

describe('the panel still distinguishes its three states', () => {
  it('an error is an error, not an empty measurement', () => {
    render(<MetricsPanel metrics={null} error={'boom'} />)

    const panel = screen.getByLabelText('Metrics')
    expect(within(panel).getByRole('alert').textContent).toContain('boom')
    expect(screen.queryByText('Runs measured')).toBeNull()
  })

  it('loading is loading, not zero runs', () => {
    render(<MetricsPanel metrics={null} error={null} />)

    expect(screen.getByText(/reading the run history/i)).toBeTruthy()
    expect(screen.queryByText('Runs measured')).toBeNull()
  })
})
