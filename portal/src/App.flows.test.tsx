import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { emptyGate, emptyMetrics, pendingFeedItem } from './fixtures'
import { installFetchMock, jsonResponse, type FetchCall } from './testUtils'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('gate toggle', () => {
  it('reflects current state, PUTs the flipped value on click, and shows the result', async () => {
    let gateEnabled = false
    const { calls } = installFetchMock({
      'GET /api/feed': () => jsonResponse({ runs: [] }),
      'GET /api/settings/gate': () => jsonResponse(emptyGate(gateEnabled)),
      'GET /api/metrics': () => jsonResponse(emptyMetrics()),
      'PUT /api/settings/gate': (call) => {
        const body = call.body as { enabled: boolean }
        gateEnabled = body.enabled
        return jsonResponse(emptyGate(gateEnabled))
      },
    })

    render(<App />)

    const toggle = (await screen.findByRole('switch')) as HTMLInputElement
    expect(toggle.checked).toBe(false)

    fireEvent.click(toggle)

    // UI reflects the PUT's result.
    await screen.findByText(/gate is on/i)
    expect(toggle.checked).toBe(true)

    const putCalls = calls.filter((c: FetchCall) => c.method === 'PUT' && c.url.includes('/api/settings/gate'))
    expect(putCalls).toHaveLength(1)
    expect(putCalls[0].body).toEqual({ enabled: true })
  })
})

describe('edit-then-approve', () => {
  it('sends the EDITED body, not the original draft', async () => {
    const item = pendingFeedItem({
      draft_body: 'Original draft body.',
      edited_body: null,
    })

    const { calls } = installFetchMock({
      'GET /api/feed': () => jsonResponse({ runs: [item] }),
      'GET /api/settings/gate': () => jsonResponse(emptyGate(true)),
      'GET /api/metrics': () => jsonResponse(emptyMetrics()),
      'PUT /api/drafts/1': () =>
        jsonResponse({
          draft_id: 1,
          run_id: 1,
          ticket_id: 'T-100',
          status: 'pending',
          body: item.draft_body,
          edited_body: 'This is the reviewer-edited reply.',
          sent_body: null,
        }),
      'POST /api/drafts/1/approve': () =>
        jsonResponse({
          draft_id: 1,
          run_id: 1,
          ticket_id: 'T-100',
          status: 'approved',
          body: item.draft_body,
          edited_body: 'This is the reviewer-edited reply.',
          sent_body: 'This is the reviewer-edited reply.',
        }),
    })

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /review/i }))

    const textarea = (await screen.findByLabelText(/reply body/i)) as HTMLTextAreaElement
    expect(textarea.value).toBe('Original draft body.')

    fireEvent.change(textarea, { target: { value: 'This is the reviewer-edited reply.' } })

    fireEvent.click(screen.getByRole('button', { name: /approve/i }))

    // Wait for the send to complete and the UI to show what was sent.
    await screen.findByText('This is the reviewer-edited reply.')

    // The critical assertion: the PUT that persisted the edit carried the
    // EDITED text as its request payload — not merely that some handler
    // fired. approve_draft (backend) sends whatever is persisted as
    // edited_body, so a UI that dropped this PUT would silently send the
    // stale original instead.
    const editCall = calls.find((c: FetchCall) => c.method === 'PUT' && c.url.includes('/api/drafts/1'))
    expect(editCall).toBeDefined()
    expect(editCall!.body).toEqual({ body: 'This is the reviewer-edited reply.' })

    const approveCall = calls.find((c: FetchCall) => c.method === 'POST' && c.url.includes('/approve'))
    expect(approveCall).toBeDefined()

    // Ordering matters: the edit must be persisted before approve sends.
    expect(calls.indexOf(editCall!)).toBeLessThan(calls.indexOf(approveCall!))
  })
})

describe('reject', () => {
  it('sends nothing — no edit or approve call is made', async () => {
    const item = pendingFeedItem()

    const { calls } = installFetchMock({
      'GET /api/feed': () => jsonResponse({ runs: [item] }),
      'GET /api/settings/gate': () => jsonResponse(emptyGate(true)),
      'GET /api/metrics': () => jsonResponse(emptyMetrics()),
      'POST /api/drafts/1/reject': () =>
        jsonResponse({
          draft_id: 1,
          run_id: 1,
          ticket_id: 'T-100',
          status: 'rejected',
          body: item.draft_body,
          edited_body: null,
          sent_body: null,
        }),
    })

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /review/i }))
    fireEvent.click(await screen.findByRole('button', { name: /reject/i }))

    await screen.findByText(/nothing was sent/i)

    expect(calls.some((c: FetchCall) => c.method === 'PUT' && c.url.includes('/api/drafts/1'))).toBe(false)
    expect(calls.some((c: FetchCall) => c.method === 'POST' && c.url.includes('/approve'))).toBe(false)
    expect(calls.filter((c: FetchCall) => c.url.includes('/reject'))).toHaveLength(1)
  })
})

describe('conflict handling', () => {
  it('surfaces a 409 (already-decided draft) as an error instead of appearing to succeed', async () => {
    const item = pendingFeedItem()

    const { calls } = installFetchMock({
      'GET /api/feed': () => jsonResponse({ runs: [item] }),
      'GET /api/settings/gate': () => jsonResponse(emptyGate(true)),
      'GET /api/metrics': () => jsonResponse(emptyMetrics()),
      'POST /api/drafts/1/approve': () =>
        jsonResponse({ detail: 'draft 1 is not pending (status=rejected)' }, 409),
    })

    render(<App />)

    fireEvent.click(await screen.findByRole('button', { name: /review/i }))
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/not pending/i)

    // It must not silently look like it worked: the draft stays visibly
    // pending (editable form still shown), no "Sent" confirmation appears.
    expect(screen.getByLabelText(/reply body/i)).toBeInTheDocument()
    expect(screen.queryByText(/^Sent$/)).not.toBeInTheDocument()

    expect(calls.filter((c: FetchCall) => c.url.includes('/approve'))).toHaveLength(1)
  })
})
