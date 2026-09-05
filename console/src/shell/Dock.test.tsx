import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import type { RequestRecord } from '../control/state'
import { Dock } from './Dock'

const now = 1_756_700_000_000

function pendingRecord(expiresAt?: number): RequestRecord {
  return {
    intent: {
      v: 1,
      t: now,
      type: 'intent',
      intent_id: 'dock-intent',
      retry_of: null,
      source: 'console',
      session: 'dock-session',
      name: 'capture_room',
      args: { room_id: 'room-01', capture_id: 'capture-dock-intent', pattern: 'pano_360' },
      selection: [1],
      mode: 'indoor',
      confirm: false,
    },
    status: 'pending_confirmation',
    timestamps: { draft: now, pending_confirmation: now },
    plan: {
      title: 'D-01 · pano_360',
      rosterVersion: 7,
      steps: ['Submit one confirmed capture_room outcome request.'],
      expiresAt,
    },
  }
}

describe('pending dock', () => {
  test('renders the countdown only when the pending plan carries a deadline', () => {
    const { rerender } = render(
      <Dock pending={pendingRecord()} invalidation={null} now={now} onConfirm={vi.fn()} onCancel={vi.fn()} />,
    )
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(dock).not.toHaveTextContent(/confirm within/)

    rerender(
      <Dock
        pending={pendingRecord(now + 40_000)}
        invalidation={null}
        now={now}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    const countdown = screen.getByText(/confirm within 40 s/)
    expect(countdown).toHaveAttribute('aria-hidden', 'true')
    expect(countdown).not.toHaveClass('is-urgent')

    rerender(
      <Dock
        pending={pendingRecord(now + 40_000)}
        invalidation={null}
        now={now + 31_000}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    expect(screen.getByText(/confirm within 9 s/)).toHaveClass('is-urgent')

    rerender(
      <Dock
        pending={pendingRecord(now + 40_000)}
        invalidation={null}
        now={now + 90_000}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    expect(screen.getByText(/confirm within 0 s/)).toBeInTheDocument()
  })

  test('expands the exact Intent v1 JSON by default and calls back with the intent id', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    const user = userEvent.setup()
    render(
      <Dock pending={pendingRecord()} invalidation={null} now={now} onConfirm={onConfirm} onCancel={onCancel} />,
    )
    const dock = screen.getByRole('region', { name: 'Pending confirmation' })
    expect(within(dock).getByRole('button', { name: 'Hide Intent v1 envelope' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    expect(within(dock).getByText(/"capture_id": "capture-dock-intent"/).tagName).toBe('PRE')

    await user.click(within(dock).getByRole('button', { name: 'Confirm and send' }))
    expect(onConfirm).toHaveBeenCalledWith('dock-intent')
    await user.click(within(dock).getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledWith('dock-intent')
  })

  test('shows the invalidation reason when nothing is pending', () => {
    render(
      <Dock
        pending={null}
        invalidation={{ reasonCode: 'stale_roster', detail: 'Fleet roster changed to version 8.' }}
        now={now}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Preview invalidated, nothing sent')
    expect(alert).toHaveTextContent('stale_roster')
    expect(alert).toHaveTextContent('Fleet roster changed to version 8.')
  })
})
