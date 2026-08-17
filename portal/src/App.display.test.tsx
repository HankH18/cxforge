import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { emptyGate, emptyMetrics, pendingFeedItem } from './fixtures'
import { installFetchMock, jsonResponse } from './testUtils'

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Mounts App with a feed of exactly `runs` and inert gate/metrics. */
function renderFeed(runs: ReturnType<typeof pendingFeedItem>[]) {
  installFetchMock({
    'GET /api/feed': () => jsonResponse({ runs }),
    'GET /api/settings/gate': () => jsonResponse(emptyGate(true)),
    'GET /api/metrics': () => jsonResponse(emptyMetrics()),
  })
  render(<App />)
}

async function rowFor(ticketId: string): Promise<HTMLTableRowElement> {
  const cell = await screen.findByText(ticketId)
  const row = cell.closest('tr')
  if (!row) throw new Error(`no <tr> ancestor for ${ticketId}`)
  return row as HTMLTableRowElement
}

// D3. `act` records EVERY run's draft with status "auto_sent"
// (backend/src/agent/nodes.py, the `store.record_draft(..., status="auto_sent")` call at
// the end of `act` — cited by symbol, not line, because that file moves) — including
// escalations and off-topic
// replies, where the sent text is only an acknowledgement and a human still
// owns the ticket. `runs.outcome` is the authoritative record of what the
// run actually did. A feed that reads draft_status alone tells the reviewer
// that an escalation was handled autonomously, which is the single most
// misleading thing this screen could say.
describe('feed renders run.outcome', () => {
  it('shows an escalated run as escalated, not as auto-sent', async () => {
    renderFeed([
      pendingFeedItem({
        run_id: 7,
        ticket_id: 'T-700',
        outcome: 'escalated',
        draft_id: 7,
        draft_status: 'auto_sent',
        draft_body: 'A specialist will follow up.',
        sent_body: 'A specialist will follow up.',
        escalation_reason: 'low_confidence',
      }),
    ])

    const row = await rowFor('T-700')
    expect(row.textContent).toMatch(/escalated/i)
    expect(row.textContent).not.toMatch(/auto-sent/i)
  })

  it('shows an off_topic run as off-topic, not as auto-sent', async () => {
    renderFeed([
      pendingFeedItem({
        run_id: 8,
        ticket_id: 'T-800',
        route: 'off_topic',
        outcome: 'off_topic',
        draft_id: 8,
        draft_status: 'auto_sent',
        draft_body: 'We only handle Othram case questions here.',
        sent_body: 'We only handle Othram case questions here.',
      }),
    ])

    const row = await rowFor('T-800')
    expect(row.textContent).toMatch(/off-topic/i)
    expect(row.textContent).not.toMatch(/auto-sent/i)
  })

  it('still calls a genuinely auto-sent run auto-sent', async () => {
    renderFeed([
      pendingFeedItem({
        run_id: 9,
        ticket_id: 'T-900',
        route: 'case_status',
        outcome: 'auto_sent',
        draft_id: 9,
        draft_status: 'auto_sent',
        sent_body: 'Your case is in extraction.',
      }),
    ])

    const row = await rowFor('T-900')
    expect(row.textContent).toMatch(/auto-sent/i)
  })
})

// D4. `runs.find((run) => run.draft_id === selectedDraftId)` matches a run
// whose draft_id is null against a null selection, so a draftless run pops
// the detail pane open on load with nothing selected.
describe('draft selection', () => {
  it('opens no detail pane when nothing is selected and a run has no draft', async () => {
    renderFeed([
      pendingFeedItem({
        run_id: 11,
        ticket_id: 'T-1100',
        outcome: 'escalated',
        draft_id: null,
        draft_status: null,
        draft_body: null,
        sent_body: null,
        escalation_reason: 'no_kb_match',
      }),
    ])

    await rowFor('T-1100')
    expect(screen.queryByRole('region', { name: /draft detail/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/no draft exists for this run/i)).not.toBeInTheDocument()
  })

  it('still opens the detail pane for the draft the reviewer picked', async () => {
    renderFeed([pendingFeedItem({ ticket_id: 'T-100' })])

    fireEvent.click(await screen.findByRole('button', { name: /review/i }))

    expect(await screen.findByRole('region', { name: /draft detail/i })).toBeInTheDocument()
  })
})
