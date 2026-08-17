import { useCallback, useEffect, useState } from 'react'

import { fetchFeed, getGate, getMetrics, type DraftResponse, type DraftStatus, type FeedItem, type MetricsResponse } from './api'
import DraftDetail from './components/DraftDetail'
import Feed from './components/Feed'
import GateToggle from './components/GateToggle'
import MetricsPanel from './components/MetricsPanel'
import { describeError } from './format'

// Feed/metrics polling cadence — R10/R13 need fresh data but the SPEC
// non-goal explicitly rules out websockets, so plain polling is enough.
const POLL_INTERVAL_MS = 5000

export default function App() {
  const [runs, setRuns] = useState<FeedItem[]>([])
  const [feedError, setFeedError] = useState<string | null>(null)
  // First load only — subsequent poll ticks must not flip the feed back to
  // a loading placeholder under the reviewer.
  const [feedLoaded, setFeedLoaded] = useState(false)
  const [statusFilter, setStatusFilter] = useState<DraftStatus | ''>('')

  const [gate, setGate] = useState<boolean | null>(null)
  const [gateError, setGateError] = useState<string | null>(null)

  const [metrics, setMetrics] = useState<MetricsResponse | null>(null)
  const [metricsError, setMetricsError] = useState<string | null>(null)

  const [selectedDraftId, setSelectedDraftId] = useState<number | null>(null)

  const loadFeed = useCallback(async () => {
    try {
      const response = await fetchFeed(statusFilter || undefined)
      setRuns(response.runs)
      setFeedError(null)
    } catch (err) {
      setFeedError(describeError(err))
    } finally {
      setFeedLoaded(true)
    }
  }, [statusFilter])

  const loadMetrics = useCallback(async () => {
    try {
      const response = await getMetrics()
      setMetrics(response)
      setMetricsError(null)
    } catch (err) {
      setMetricsError(describeError(err))
    }
  }, [])

  // Gate setting is fetched once up front; GateToggle updates it locally
  // after a successful PUT rather than waiting for the next poll.
  useEffect(() => {
    let cancelled = false
    getGate()
      .then((setting) => {
        if (!cancelled) setGate(setting.enabled)
      })
      .catch((err: unknown) => {
        if (!cancelled) setGateError(describeError(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    loadFeed()
    const id = setInterval(loadFeed, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [loadFeed])

  useEffect(() => {
    loadMetrics()
    const id = setInterval(loadMetrics, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [loadMetrics])

  // `selectedDraftId === null` means NOTHING is selected. Matching it
  // against `run.draft_id` would find any draftless run — an escalation, say
  // — and pop its detail pane open unbidden on first paint. Guard the null
  // case before searching.
  const selectedItem =
    selectedDraftId === null
      ? null
      : (runs.find((run) => run.draft_id === selectedDraftId) ?? null)

  function handleDraftChanged(result: DraftResponse) {
    // Patch the row immediately so the reviewer isn't staring at stale
    // "pending" state until the next poll tick — this covers everything
    // DraftResponse actually carries (status/edited_body/sent_body). The
    // next poll tick (POLL_INTERVAL_MS) picks up server-only fields
    // approve/reject also affect (`run.outcome`, escalation reasons) — no
    // need to force a full feed re-fetch here, which would just race the
    // optimistic patch for no benefit. Metrics DO get an immediate refresh:
    // the human-avoidance numerator changes on every approve/reject and
    // that is worth showing right away rather than waiting up to
    // POLL_INTERVAL_MS.
    setRuns((prev) =>
      prev.map((run) =>
        run.draft_id === result.draft_id
          ? {
              ...run,
              draft_status: result.status,
              edited_body: result.edited_body,
              sent_body: result.sent_body ?? run.sent_body,
            }
          : run,
      ),
    )
    loadMetrics()
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__title">Othram Support — Review Portal</h1>
        <p className="app__subtitle">
          Every agent run, the reply it drafted, and who decided to send it.
        </p>
      </header>

      <main className="app__main">
        <div className="app__columns">
          <div className="app__column app__column--gate">
            <GateToggle
              enabled={gate}
              onChanged={(enabled) => {
                setGate(enabled)
                setGateError(null)
              }}
            />
            {gateError && (
              <p className="alert alert--error" role="alert">
                Could not load gate setting: {gateError}
              </p>
            )}
          </div>

          <div className="app__column app__column--metrics">
            <MetricsPanel metrics={metrics} error={metricsError} />
          </div>
        </div>

        <Feed
          runs={runs}
          error={feedError}
          loading={!feedLoaded}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          selectedDraftId={selectedDraftId}
          onSelect={(item) => setSelectedDraftId(item.draft_id)}
        />

        {selectedItem && (
          <DraftDetail
            item={selectedItem}
            onClose={() => setSelectedDraftId(null)}
            onChanged={handleDraftChanged}
          />
        )}
      </main>

      <footer className="app__footer">
        <p>Feed and metrics refresh every {POLL_INTERVAL_MS / 1000} seconds.</p>
      </footer>
    </div>
  )
}
