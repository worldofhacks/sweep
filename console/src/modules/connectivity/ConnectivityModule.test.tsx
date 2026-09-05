import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import { openReferenceTab, renderCatalogConsole } from '../../testing/catalog-console'

type User = ReturnType<typeof userEvent.setup>

async function openHealth(user: User) {
  await openReferenceTab(user, 'Health')
  expect(screen.getByText(/Connectivity and health — Nodes, services, metrics/)).toBeInTheDocument()
}

function cell(row: HTMLElement, key: string): HTMLElement {
  const term = within(row).getByText(key, { selector: 'dt' })
  return term.nextElementSibling as HTMLElement
}

describe('Connectivity module', () => {
  test('populated: nine metric tiles, nine cells per node with error lines, shared services, and the ladder', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'six6' })
    await openHealth(user)

    const metrics = within(screen.getByRole('region', { name: 'Health metrics' }))
    expect(metrics.getByText('unsafe commands dispatched').nextElementSibling).toHaveTextContent('0')
    expect(metrics.getByText('refusals this session').nextElementSibling).toHaveClass('tone-warn')
    expect(metrics.getAllByText(/^(0|41 ms|84 ms|118 ms|29\.4 Hz|210 ms|640 ms|6|0\.4 \/ 5 min)$/)).toHaveLength(9)

    const table = screen.getByRole('table', {
      name: /Every cell answers what is wrong and what to do/,
    })
    const rows = within(table).getAllByRole('row')
    expect(rows.map((row) => within(row).getByRole('rowheader').textContent)).toEqual([
      'D-01',
      'D-02',
      'D-03',
      'D-04',
      'D-05',
      'D-06',
    ])
    expect(within(rows[0]).getAllByRole('term').map((term) => term.textContent)).toEqual([
      'RC controller',
      'Android bridge',
      'LAN',
      'Relay',
      'Telemetry',
      'Camera',
      'Video',
      'Storage',
      'Firmware',
    ])
    expect(cell(rows[0], 'RC controller')).toHaveTextContent('standby · fw 2.4.1')
    expect(cell(rows[0], 'Android bridge')).toHaveTextContent('Pixel 7a · sdk 1.3.0')
    expect(cell(rows[0], 'LAN')).toHaveTextContent('18 ms')
    expect(cell(rows[0], 'Relay')).toHaveTextContent('connected')
    expect(cell(rows[0], 'Telemetry')).toHaveTextContent('29.4 Hz')
    expect(cell(rows[0], 'Camera')).toHaveTextContent('ready · 2 patterns')
    expect(cell(rows[0], 'Video')).toHaveTextContent(/^live · /)
    expect(cell(rows[0], 'Storage')).toHaveTextContent('15 GB free')
    expect(cell(rows[0], 'Firmware')).toHaveTextContent('aircraft 0.9.7')
    expect(within(rows[0]).queryByText(/Adapter connection lost|Telemetry stopped|RC pilot/)).not.toBeInTheDocument()

    expect(cell(rows[2], 'Telemetry')).toHaveTextContent(/^stale /)
    expect(cell(rows[2], 'Telemetry')).toHaveClass('tone-warn')
    expect(
      within(rows[2]).getByText("Telemetry stopped. Check the bridge phone's LAN link before commanding motion."),
    ).toBeInTheDocument()

    expect(cell(rows[3], 'RC controller')).toHaveTextContent('in control · fw 2.4.1')
    expect(cell(rows[3], 'RC controller')).toHaveClass('tone-danger')
    expect(
      within(rows[3]).getByText('The RC pilot holds authority. Sweep commands are refused until authority returns.'),
    ).toBeInTheDocument()

    expect(cell(rows[4], 'Android bridge')).toHaveTextContent('down')
    expect(cell(rows[4], 'LAN')).toHaveTextContent('no route')
    expect(cell(rows[4], 'Relay')).toHaveTextContent('disconnected')
    expect(cell(rows[4], 'Storage')).toHaveTextContent('unknown')
    expect(
      within(rows[4]).getByText(
        'Adapter connection lost. Power-cycle the bridge phone, then rejoin; the aircraft returns with a higher epoch.',
      ),
    ).toBeInTheDocument()

    const services = within(screen.getByRole('region', { name: 'Shared services' }))
    expect(services.getAllByRole('listitem').map((item) => item.firstElementChild?.textContent)).toEqual([
      'Relay',
      'Keyboard stop',
      'Media server',
      'World API',
      'Storage',
    ])
    expect(services.getByText('Relay').nextElementSibling).toHaveTextContent('degraded')
    expect(services.getByText('Keyboard stop').nextElementSibling).toHaveTextContent('connected')
    expect(services.getByText('6 streams named drone1…drone6')).toBeInTheDocument()
    expect(services.getByText('All submissions carry public false.')).toBeInTheDocument()

    const ladder = within(screen.getByRole('list', { name: 'Degradation ladder rungs' }))
    expect(ladder.getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'full',
      'no video',
      'no language',
      'webcam only',
      'keyboard stop only',
    ])
    expect(ladder.getByText('full')).toHaveAttribute('aria-current', 'true')
    expect(screen.getByText('Current rung: full.')).toBeInTheDocument()
  })

  test('empty: an empty catalog reports no metrics or extra services while the live rows stay', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4 })
    await openHealth(user)

    expect(screen.getByText('No health metrics are reported for this session yet.')).toBeInTheDocument()
    const services = within(screen.getByRole('region', { name: 'Shared services' }))
    expect(services.getAllByRole('listitem')).toHaveLength(2)
    expect(services.getByText('No shared services beyond the relay sockets are reported.')).toBeInTheDocument()
    expect(services.getByText('Two sockets authenticated: console and keyboard.')).toBeInTheDocument()

    const table = screen.getByRole('table')
    const rows = within(table).getAllByRole('row')
    expect(rows).toHaveLength(4)
    expect(cell(rows[0], 'Android bridge')).toHaveTextContent('unreported')
    expect(cell(rows[0], 'LAN')).toHaveTextContent('unreported')
    expect(cell(rows[0], 'Firmware')).toHaveTextContent('unreported')
    expect(cell(rows[0], 'Relay')).toHaveTextContent('connected')
  })

  test('degraded: both sockets down leaves no rung held and marks the relay rows danger', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'down' })
    await openHealth(user)

    const nodes = within(screen.getByRole('region', { name: 'Per-aircraft nodes' }))
    expect(
      nodes.getByText('No aircraft have joined this session. The relay reports an empty roster.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    const services = within(screen.getByRole('region', { name: 'Shared services' }))
    expect(services.getByText('Relay').nextElementSibling).toHaveTextContent('disconnected')
    expect(services.getByText('Relay').nextElementSibling).toHaveClass('tone-danger')
    expect(services.getByText('Keyboard stop').nextElementSibling).toHaveTextContent('disconnected')
    const ladder = within(screen.getByRole('list', { name: 'Degradation ladder rungs' }))
    ladder.getAllByRole('listitem').forEach((item) => expect(item).not.toHaveAttribute('aria-current'))
    expect(
      screen.getByText('Both sockets are disconnected; no rung is held. The physical RC remains primary.'),
    ).toBeInTheDocument()
  })

  test('unreported: production keeps the relay rows and says metrics and services are unreported', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4, catalog: 'unreported' })
    await openHealth(user)

    expect(screen.getByText(/does not report health metrics/).closest('[role="status"]')).toHaveTextContent(
      'Nothing to show',
    )
    expect(screen.getByText(/does not report shared-service status/)).toBeInTheDocument()
    const rows = within(screen.getByRole('table')).getAllByRole('row')
    expect(rows).toHaveLength(4)
    expect(cell(rows[0], 'RC controller')).toHaveTextContent('standby · fw unreported')
    expect(cell(rows[0], 'Telemetry')).toHaveTextContent('unreported')
  })
})
