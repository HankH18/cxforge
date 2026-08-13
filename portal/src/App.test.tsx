import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { emptyGate, emptyMetrics } from './fixtures'
import { installFetchMock, jsonResponse } from './testUtils'

// Harness smoke test — proves `npm test` runs React component tests, which
// is what T-9's gate and edit-approve flow tests (src/App.flows.test.tsx)
// need. Fetch is mocked (never a real backend, per T-9's brief) with
// empty-but-valid responses so mount doesn't error.
describe('App', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the portal shell', async () => {
    installFetchMock({
      'GET /api/feed': () => jsonResponse({ runs: [] }),
      'GET /api/settings/gate': () => jsonResponse(emptyGate()),
      'GET /api/metrics': () => jsonResponse(emptyMetrics()),
    })

    render(<App />)
    expect(screen.getByRole('heading', { name: /review portal/i })).toBeInTheDocument()

    // Let the mocked mount-time fetches settle inside React's act() so no
    // state update lands after the test has already finished.
    await screen.findByText(/no runs yet/i)
  })
})
