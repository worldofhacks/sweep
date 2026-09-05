import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import { MEMBERSHIP_REASON, READINESS, REASONS } from '../../shell/sentences'
import { openReferenceTab, renderCatalogConsole } from '../../testing/catalog-console'

describe('Reference module', () => {
  test('states gallery: thirteen vocabulary domains with the design colour key, then the three reason tables', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openReferenceTab(user, 'States')
    expect(screen.getByText(/States gallery — Every vocabulary value/)).toBeInTheDocument()

    const domains = screen.getAllByRole('region', { name: /vocabulary$/ })
    expect(domains.map((domain) => domain.getAttribute('aria-label'))).toEqual([
      'Connection vocabulary',
      'Membership vocabulary',
      'Membership events vocabulary',
      'Flight state vocabulary',
      'Intent lifecycle vocabulary',
      'Stream status vocabulary',
      'Capture pattern vocabulary',
      'Coverage vocabulary',
      'Capture progress vocabulary',
      'Guidance mode vocabulary',
      'Generation job vocabulary',
      'Mode vocabulary',
      'Provenance vocabulary',
    ])

    const lifecycle = within(screen.getByRole('region', { name: 'Intent lifecycle vocabulary' }))
    expect(lifecycle.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      '■draft',
      '■pending_confirmation',
      '■sent',
      '■accepted',
      '■refused',
      '■executing',
      '■completed',
      '■failed',
      '■invalidated',
      '■cancelled',
    ])
    expect(lifecycle.getByText('completed', { exact: false })).toHaveClass('tone-ok')
    expect(lifecycle.getByText('refused', { exact: false })).toHaveClass('tone-danger')
    expect(lifecycle.getByText('draft', { exact: false })).toHaveClass('tone-muted')

    const modes = within(screen.getByRole('region', { name: 'Mode vocabulary' }))
    expect(modes.getByText('outdoorC', { exact: false })).toHaveTextContent('unsupported')
    expect(modes.getByText('indoor', { exact: false })).not.toHaveTextContent('unsupported')

    const jobs = within(screen.getByRole('region', { name: 'Generation job vocabulary' }))
    expect(jobs.getByText('running', { exact: false })).toHaveClass('tone-warn')
    expect(jobs.getByText('timed_out', { exact: false })).toHaveClass('tone-danger')

    const refusals = within(screen.getByRole('region', { name: 'Refusal and failure reasons' }))
    expect(refusals.getAllByRole('listitem')).toHaveLength(Object.keys(REASONS).length)
    expect(Object.keys(REASONS)).toHaveLength(48)
    expect(refusals.getByText('estop_active')).toBeInTheDocument()
    expect(refusals.getByText('estop_active').closest('li')).toHaveTextContent(REASONS.estop_active)
    expect(refusals.getByText('estop_active').closest('li')).toHaveClass('is-danger')

    const readiness = within(screen.getByRole('region', { name: 'Readiness reasons' }))
    expect(readiness.getAllByRole('listitem')).toHaveLength(Object.keys(READINESS).length)
    expect(Object.keys(READINESS)).toHaveLength(10)
    const membership = within(screen.getByRole('region', { name: 'Membership reasons' }))
    expect(membership.getAllByRole('listitem')).toHaveLength(Object.keys(MEMBERSHIP_REASON).length)
    expect(Object.keys(MEMBERSHIP_REASON)).toHaveLength(8)
  })

  test('the gallery is the console vocabulary, so it renders the same without a catalog', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4, catalog: 'unreported' })
    await openReferenceTab(user, 'States')
    expect(screen.getAllByRole('region', { name: /vocabulary$/ })).toHaveLength(13)
    expect(screen.queryByText(/does not report/)).not.toBeInTheDocument()
  })

  test('Mission is the Appendix E tracker; Ledger and Map stay honest empties until the relay feeds them', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openReferenceTab(user, 'Mission')
    expect(screen.getByLabelText('Elapsed')).toHaveTextContent('0:00')
    expect(screen.getAllByRole('button', { name: /accepted at M2\.0|unsupported/ })).toHaveLength(10)
    expect(screen.queryByText(/does not report a mission tracker/)).not.toBeInTheDocument()
    await openReferenceTab(user, 'Ledger')
    expect(screen.getByText(/does not report a session ledger or replay/)).toBeInTheDocument()
    await openReferenceTab(user, 'Map')
    expect(screen.getByText(/does not report positions or a room graph/)).toBeInTheDocument()
  })
})
