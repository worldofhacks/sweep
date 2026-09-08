import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import App from '../../App'
import type { NavigationCatalog, NavigationClient } from '../../navigation'
import type { MultiviewClient, MultiviewPreviewRequest, MultiviewPreview } from '../../relay/multiview'
import { C1_BASIC_CONTROL_INTENTS } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'

const time = 1_756_700_000_000
const pin = { version: 'v1', contentSha256: 'a'.repeat(64) }
const catalog: NavigationCatalog = {
  session: 'photo-test', catalogVersion: 'v1', receivedAt: time, expiresAt: time + 60_000,
  map: { mapId: 'test-map', floorId: 'floor', frame: 'world', accepted: true, approvalId: 'approval', mapPin: pin, geometryPin: pin, navigationPin: pin },
  configVersion: 'motion-v1', motionConfig: { fixture: true },
  destinations: ['lobby', 'atrium'].map((zoneId) => ({ zoneId, name: zoneId, aliases: [], floorId: 'floor', excluded: false, reachability: 'reachable', allowedClasses: ['aircraft'] })),
}
function reviewed(request: MultiviewPreviewRequest): MultiviewPreview {
  return { previewId: 'review-1', intentId: request.intentId, previewHash: 'b'.repeat(64), expiresAt: time + 15_000,
    execution: { planHash: 'c'.repeat(64), mapPin: pin, geometryPin: pin, navigationPin: pin, approvalId: 'approval', configurationSha256: 'd'.repeat(64), permissionZoneIds: ['atrium', 'lobby'] },
    views: request.viewpoints.map((view, index) => {
      const start = { xM: index, yM: 0, zM: 1, floorId: 'floor', frame: 'world' as const }
      const end = { ...start, xM: index + 1 }
      return { ...view, route: { target: request.selected[0], waypoints: [start, end], arrivalSlot: { slotId: view.zoneId, zoneId: view.zoneId, position: end }, holdBehavior: 'hover' }, capture: { roomId: view.zoneId, pattern: 'single_still' } }
    }) }
}
function mount() {
  const snapshot: ReturnType<NavigationClient['getSnapshot']> = { status: 'ready', reason: null, catalog, preview: null, reviewSupported: true }
  const navigation: NavigationClient = {
    getSnapshot: () => snapshot,
    subscribe: () => () => {}, requestPreview: async () => { throw new Error('unused') },
  }
  const preview = vi.fn(async (request: MultiviewPreviewRequest) => reviewed(request))
  const confirm = vi.fn(async (value: MultiviewPreview) => value.previewId)
  const status = vi.fn<MultiviewClient['status']>(async (value) => ({ workflowId: value.previewId, intentId: value.intentId, status: 'completed',
    views: value.views.map((view) => ({ ...view, state: 'completed', detail: 'Photo retrieved.' })) }))
  const clients = { console: new FixtureRelayClient('photo-test', () => time, 'console'), keyboard: new FixtureRelayClient('photo-test', () => time, 'keyboard') }
  render(<App sessionId="photo-test" clients={clients} initialModule="captures" intentDependencies={{ now: () => time, nextId: () => crypto.randomUUID() }} services={{ navigation, multiview: { preview, confirm, status } }} />)
  const emit = (epoch = 1) => act(() => clients.console.emitServer({ v: 1, t: time, type: 'state', event_id: `state-${epoch}`, session: 'photo-test', roster_version: 100 + epoch, state_sequence: 100 + epoch,
    armed: true, estop: false, selection: [1], formation: 'none', spacing: 0.8, mode: 'indoor', pending: null, accepted_plan: null,
    capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
    drones: fixtureAircraft(time).map((device) => ({ ...device, connection_epoch: epoch, flight_state: 'hovering' })),
  }))
  emit()
  return { preview, confirm, status, clients, emit }
}

test('shows ordered movement before confirming and reports retrieved photographs', async () => {
  const fixture = mount()
  const user = userEvent.setup()
  await user.selectOptions(await screen.findByLabelText('Photo stop 1'), 'lobby')
  await user.selectOptions(screen.getByLabelText('Photo stop 2'), 'atrium')
  await user.click(screen.getByRole('button', { name: 'Preview photo route' }))
  const review = await screen.findByLabelText('Photo route review')
  expect(within(review).getByRole('img', { name: /Reviewed photo route/ })).toBeVisible()
  expect(review).toHaveTextContent('Hold at 2.00, 0.00, 1.00 m')
  expect(fixture.confirm).not.toHaveBeenCalled()
  expect(fixture.clients.console.sent).toHaveLength(0)
  await user.click(within(review).getByRole('button', { name: 'Confirm photo route' }))
  expect(fixture.confirm).toHaveBeenCalledWith(await fixture.preview.mock.results[0].value)
  expect(await screen.findByText('Photo route: completed')).toBeVisible()
  expect(screen.getAllByText(/Photo retrieved/)).toHaveLength(2)
})

test('a changed aircraft epoch disables the visible confirmation', async () => {
  const fixture = mount()
  const user = userEvent.setup()
  await user.selectOptions(await screen.findByLabelText('Photo stop 1'), 'lobby')
  await user.selectOptions(screen.getByLabelText('Photo stop 2'), 'atrium')
  await user.click(screen.getByRole('button', { name: 'Preview photo route' }))
  expect(await screen.findByRole('button', { name: 'Confirm photo route' })).toBeEnabled()
  fixture.emit(2)
  expect(screen.getByRole('button', { name: 'Confirm photo route' })).toBeDisabled()
  expect(fixture.confirm).not.toHaveBeenCalled()
})
