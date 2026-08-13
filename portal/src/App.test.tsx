import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'

// Harness smoke test — proves `npm test` runs React component tests, which is
// what T-9's gate and edit-approve flow tests will need.
describe('App', () => {
  it('renders the portal shell', () => {
    render(<App />)
    expect(screen.getByRole('heading', { name: /review portal/i })).toBeInTheDocument()
  })
})
