import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test } from 'vitest'
import {
  openModule,
  openPaneTab,
  openReferenceTab,
  renderCatalogConsole,
} from '../../testing/catalog-console'

type User = ReturnType<typeof userEvent.setup>

async function openConfig(user: User) {
  await openReferenceTab(user, 'Config')
  expect(screen.getByText(/Configuration — Ordinary settings apply now/)).toBeInTheDocument()
}

function group(title: string) {
  return within(screen.getByRole('region', { name: title }))
}

describe('Configuration module', () => {
  test('populated: seven groups with apply-now versus staged semantics, and the three modes', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openConfig(user)

    expect(
      screen.getByText(
        'A configuration change while a plan is active invalidates that plan. The form warns before the change, and the shell states the invalidation.',
      ),
    ).toBeInTheDocument()
    const titles = screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)
    expect(titles).toEqual([
      'Input device',
      'Camera',
      'Capture pattern defaults',
      'World API',
      'Media',
      'Thresholds',
      'Connection',
      'Modes',
    ])

    const camera = group('Camera')
    expect(camera.getByText('live')).toHaveClass('tone-ok')
    expect(camera.getByText('Applies now.')).toBeInTheDocument()
    expect(camera.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(camera.getByLabelText('Gimbal pitch default')).toHaveValue('−12°')

    const thresholds = group('Thresholds')
    expect(thresholds.getByText('pending until the next run')).toHaveClass('tone-warn')
    expect(thresholds.getByText('Safety-sensitive. Staged and applied between runs.')).toBeInTheDocument()
    expect(thresholds.getByRole('button', { name: 'Stage for the next run' })).toBeDisabled()

    const modes = within(screen.getByRole('region', { name: 'Modes' }))
    expect(modes.getByText('indoor').nextElementSibling).toHaveTextContent('accepted')
    expect(modes.getByText('outdoorC').nextElementSibling).toHaveTextContent('unsupported')
    expect(modes.getByText('outdoorC').nextElementSibling).toHaveClass('tone-warn')
    expect(modes.getByText('positioning GPS plus compass')).toBeInTheDocument()
    expect(modes.getByText('box moving fence around the operator')).toBeInTheDocument()
    expect(modes.getByText('spacing 0.8 m')).toBeInTheDocument()
    expect(modes.getByText('speed 6 m/s')).toBeInTheDocument()
  })

  test('apply now: saving without a pending plan applies the value at once', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4' })
    await openConfig(user)

    const camera = group('Camera')
    const exposure = camera.getByLabelText('Exposure')
    await user.clear(exposure)
    await user.type(exposure, 'manual')
    await user.click(camera.getByRole('button', { name: 'Save' }))
    expect(screen.getByRole('status', { name: 'Configuration notice' })).toHaveTextContent(
      'Saved Camera — 1 value applied now.',
    )
    expect(camera.getByLabelText('Exposure')).toHaveValue('manual')
    expect(camera.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  test('staged: a safety-sensitive change is staged for the next run and never touches a pending plan', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'pending4', nextId: () => 'config-staged-plan' })
    await openPaneTab(user, 'Control panes', 'Capture')
    await user.click(screen.getByRole('button', { name: /Capture room/ }))
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()

    await openConfig(user)
    const thresholds = group('Thresholds')
    const reserve = thresholds.getByLabelText('Battery reserve')
    await user.clear(reserve)
    await user.type(reserve, '30%')
    await user.click(thresholds.getByRole('button', { name: 'Stage for the next run' }))
    expect(screen.getByRole('status', { name: 'Configuration notice' })).toHaveTextContent(
      'Staged Thresholds — 1 value pending until the next run. Nothing changes until then.',
    )
    expect(thresholds.getByText('staged 30%')).toHaveClass('cfg-staged')
    expect(thresholds.getByLabelText(/Battery reserve/)).toHaveValue('28%')
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  test('apply now with a pending plan: the form warns first, then the shell states the invalidation', async () => {
    const user = userEvent.setup()
    const { clients } = renderCatalogConsole({ scenario: 'pending4', nextId: () => 'config-invalidated-plan' })
    await openPaneTab(user, 'Control panes', 'Capture')
    await user.click(screen.getByRole('button', { name: /Capture room/ }))
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()

    await openConfig(user)
    const media = group('Media')
    const path = media.getByLabelText('Download path')
    await user.clear(path)
    await user.type(path, 'session/media')
    await user.click(media.getByRole('button', { name: 'Save' }))

    const confirm = within(screen.getByRole('group', { name: 'Confirm Media change' }))
    expect(confirm.getByText(/Saving Media invalidates the pending plan/)).toHaveTextContent(
      'D-01 · pano_360. Nothing has been sent.',
    )
    await user.click(confirm.getByRole('button', { name: 'Keep the plan' }))
    expect(screen.queryByRole('group', { name: 'Confirm Media change' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Pending confirmation' })).toBeInTheDocument()
    expect(media.getByLabelText('Download path')).toHaveValue('session/media')

    await user.click(media.getByRole('button', { name: 'Save' }))
    await user.click(screen.getByRole('button', { name: 'Save and invalidate the plan' }))
    await waitFor(() =>
      expect(screen.getByRole('status', { name: 'Configuration notice' })).toHaveTextContent(
        'Saved Media — 1 value applied now.',
      ),
    )
    expect(screen.queryByRole('region', { name: 'Pending confirmation' })).not.toBeInTheDocument()
    const alert = screen.getByText(/Preview invalidated, nothing sent/).closest('[role="alert"]')
    expect(alert).toHaveTextContent('configuration_changed')
    expect(alert).toHaveTextContent('Configuration changed: Media. Build and confirm a new preview.')
    expect(clients.console.sent).toHaveLength(0)

    await openModule(user, 'Control')
    await openPaneTab(user, 'Control panes', 'Requests')
    expect(screen.getAllByText('Configuration changed: Media. Build and confirm a new preview.')).not.toHaveLength(0)
  })

  test('empty: an empty configuration reports no groups and no modes', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4 })
    await openConfig(user)

    expect(screen.getByText('No configuration groups are reported for this session.')).toBeInTheDocument()
    expect(screen.getByText('No modes are reported.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })

  test('degraded: the console link is down, so inputs and actions are refused while values stay', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 'down' })
    await openConfig(user)

    expect(screen.getByRole('status', { name: 'Configuration connection' })).toHaveTextContent(
      'The console connection is disconnected. Configuration shows the last snapshot received; saves and staging are refused until the relay reports connected.',
    )
    const thresholds = group('Thresholds')
    expect(thresholds.getByLabelText('Ceiling')).toHaveValue('2.4 m')
    expect(thresholds.getByLabelText('Ceiling')).toBeDisabled()
    expect(thresholds.getByRole('button', { name: 'Stage for the next run' })).toBeDisabled()
    expect(thresholds.getByRole('button', { name: 'Stage for the next run' })).toHaveAttribute(
      'title',
      'The console connection is disconnected. Nothing can be sent.',
    )
  })

  test('unreported: production has no configuration endpoint, so the module says so', async () => {
    const user = userEvent.setup()
    renderCatalogConsole({ scenario: 4, catalog: 'unreported' })
    await openConfig(user)

    expect(screen.getByText(/does not report editable configuration/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 3, name: 'Modes' })).not.toBeInTheDocument()
  })
})
