import { render, screen } from '@testing-library/react'
import { describe, expect, test } from 'vitest'
import type { OperatorNotice } from '../control/state'
import { NoticeLine } from './NoticeLine'

const t = 1_756_700_000_000

function notice(level: OperatorNotice['level']): OperatorNotice {
  return { id: `${level}-1`, level, title: 'Relay degraded', detail: 'Heartbeat late by 4 s.', t }
}

describe('notice line', () => {
  test('stays mounted and empty as a polite live region before any notice arrives', () => {
    render(<NoticeLine notice={null} />)
    const line = screen.getByRole('status', { name: 'Latest notice' })
    expect(line).toHaveAttribute('aria-live', 'polite')
    expect(line).toBeEmptyDOMElement()
    expect(line).toHaveClass('is-empty')
  })

  test('a warning fills the same element with its severity word, title, and detail', () => {
    const { rerender } = render(<NoticeLine notice={null} />)
    const line = screen.getByRole('status', { name: 'Latest notice' })
    rerender(<NoticeLine notice={notice('warning')} />)
    expect(screen.getByRole('status', { name: 'Latest notice' })).toBe(line)
    expect(line).toHaveTextContent('Warning — Relay degraded: Heartbeat late by 4 s.')
    expect(line).toHaveClass('is-warning')
    expect(line).not.toHaveClass('is-empty')
  })

  test('info notices use the quieter tone', () => {
    render(<NoticeLine notice={notice('info')} />)
    const line = screen.getByRole('status', { name: 'Latest notice' })
    expect(line).toHaveTextContent(/^Info — /)
    expect(line).toHaveClass('is-info')
  })
})
