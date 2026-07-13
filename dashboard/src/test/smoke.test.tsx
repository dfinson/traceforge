import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

// Trivial smoke test proving the Vitest + Testing Library + jsdom harness runs.
// View/logic tests live in follow-up issues (#194/#195/#196).
describe('dashboard test harness', () => {
  it('renders a component into the jsdom document', () => {
    render(<h1>traceforge dashboard</h1>)

    expect(
      screen.getByRole('heading', { name: 'traceforge dashboard' }),
    ).toBeInTheDocument()
  })
})
