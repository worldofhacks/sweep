import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import { formatTime } from '../../shell/format'
import { CATALOG_CLOCK, openModule, renderCatalogConsole } from '../../testing/catalog-console'

async function openCaptures(user: ReturnType<typeof userEvent.setup>) {
  await openModule(user, 'Captures')
  return screen.getByRole('heading', { level: 1, name: 'Capture library' })
}

describe('Captures module', () => {
  test('populated: catalog by project, room, aircraft and time with metadata and retake flag', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openCaptures(user)

    const filters = within(screen.getByRole('group', { name: 'Capture filters' }))
    expect(filters.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'All captures',
      'kitchen-01',
      'hall-02',
      'studio-03',
      'D-01',
      'D-02',
      'Needs retake',
    ])
    expect(filters.getByRole('button', { name: 'All captures' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('region', { name: 'Project ground-floor' })).toBeInTheDocument()

    const items = screen.getAllByRole('article', { name: /^Capture cap-/ })
    expect(items.map((item) => item.getAttribute('aria-label'))).toEqual([
      'Capture cap-0147',
      'Capture cap-0146',
      'Capture cap-0142',
      'Capture cap-0139',
    ])

    const retake = within(screen.getByRole('article', { name: 'Capture cap-0146' }))
    expect(retake.getByText(`kitchen-01 · D-01 · ${formatTime(CATALOG_CLOCK - 902_000)}`)).toBeInTheDocument()
    expect(retake.getByText('reconstruct_8')).toBeInTheDocument()
    expect(retake.getByText('incomplete_vertical_coverage')).toBeInTheDocument()
    expect(retake.getByText(/8 files · quality/)).toHaveTextContent('fail')
    expect(retake.getByText('fail')).toHaveClass('tone-danger')
    expect(retake.getByText('needs retake')).toBeInTheDocument()
    expect(retake.getByText('sha256:1ba07c39…44e2')).toBeInTheDocument()
    expect(
      retake.getByText('x 0.38 y 1.11 z 1.40 · yaw 44.9° · gimbal −10.5° · f 3.2 mm'),
    ).toBeInTheDocument()

    const passing = within(screen.getByRole('article', { name: 'Capture cap-0147' }))
    expect(passing.getByText(/1 file · quality/)).toHaveTextContent('pass')
    expect(passing.queryByText('needs retake')).not.toBeInTheDocument()

    await user.click(filters.getByRole('button', { name: 'D-02' }))
    expect(screen.getAllByRole('article', { name: /^Capture cap-/ })).toHaveLength(2)
    await user.click(filters.getByRole('button', { name: 'Needs retake' }))
    expect(screen.getAllByRole('article', { name: /^Capture cap-/ })).toHaveLength(1)
    await user.click(filters.getByRole('button', { name: 'studio-03' }))
    expect(screen.getByRole('article', { name: 'Capture cap-0139' })).toBeInTheDocument()
  })

  test('populated: download and export report the catalog outcome without re-encoding', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openCaptures(user)

    const item = within(screen.getByRole('article', { name: 'Capture cap-0147' }))
    await user.click(item.getByRole('button', { name: 'Download set' }))
    const note = screen.getByRole('status', { name: 'Capture library notice' })
    expect(note).toHaveTextContent(
      "cap-0147 — 1 file staged to session/captures, checksum sha256:9f2c41ab…7d10 verified against the aircraft's manifest.",
    )
    await user.click(item.getByRole('button', { name: 'Export metadata' }))
    expect(note).toHaveTextContent(
      'cap-0147.json exported: pattern, coverage label, pose, camera intrinsics, quality results and checksums. Media files are not re-encoded.',
    )
  })

  test('empty: a project with no captures shows the dashed notice under the filter strip', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4 })
    await openCaptures(user)

    const filters = within(screen.getByRole('group', { name: 'Capture filters' }))
    expect(filters.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'All captures',
      'Needs retake',
    ])
    expect(
      screen.getByText('No captures match this filter. A project with no captures shows the same notice.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('article', { name: /^Capture/ })).not.toBeInTheDocument()
  })

  test('degraded: the last snapshot stays while the console link is down and actions are refused', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'down' })
    await openCaptures(user)

    expect(screen.getByRole('status', { name: 'Capture library connection' })).toHaveTextContent(
      'The console connection is disconnected. The capture library shows the last snapshot received; downloads and exports are refused until the relay reports connected.',
    )
    expect(screen.getAllByRole('article', { name: /^Capture cap-/ })).toHaveLength(4)
    const item = within(screen.getByRole('article', { name: 'Capture cap-0147' }))
    expect(item.getByRole('button', { name: 'Download set' })).toBeDisabled()
    expect(item.getByRole('button', { name: 'Export metadata' })).toBeDisabled()
    expect(item.getByRole('button', { name: 'Download set' })).toHaveAttribute(
      'title',
      'The console connection is disconnected. Nothing can be sent.',
    )
  })

  test('unreported: production has no catalog endpoint, so the module says so', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4, catalog: 'unreported' })
    await openCaptures(user)

    const empty = screen.getByText(/The relay does not report captured media sets/)
    expect(empty.closest('[role="status"]')).toHaveTextContent('Nothing to show')
    expect(screen.queryByRole('group', { name: 'Capture filters' })).not.toBeInTheDocument()
  })
})
