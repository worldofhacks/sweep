import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'
import { navigationTargets } from '../../../control/navigation'
import { createInitialControlState, type ControlState } from '../../../control/state'
import {
  NAVIGATION_CONFIRMATION_UNAVAILABLE,
  UnavailableNavigationClient,
  parseNavigationCatalog,
  parseNavigationPreview,
  type NavigationCatalog,
  type NavigationDestination,
  type NavigationPreview,
  type NavigationSnapshot,
} from '../../../navigation'
import type { DeviceClass, RelayAircraftState } from '../../../relay/contract'
import { fixtureAircraft } from '../../../testing/fixture-relay-client'
import { NavigationPane, NavigationPreviewDetails } from './NavigationPane'

const NOW = 1_756_700_000_000
const SESSION = 'navigation-ui-test'

function device(id: number, deviceClass: DeviceClass, unit: number, epoch: number): RelayAircraftState {
  return {
    ...fixtureAircraft(NOW)[0], drone_id: id, device_class: deviceClass, unit,
    connection_epoch: epoch, flight_state: deviceClass === 'aircraft' ? 'hovering' : 'idle',
    last_seen_at: NOW, telemetry: { t: NOW, fresh: true },
  }
}

function state(classes: DeviceClass[] = ['aircraft']): ControlState {
  const devices = classes.map((kind, index) => device(index + 11, kind, index + 3, index + 7))
  return {
    ...createInitialControlState(SESSION, NOW),
    connection: { status: 'connected', transport: 'fixture', changedAt: NOW },
    capabilityProfile: 'navigation-review-test', enabledIntentNames: ['navigate'],
    rosterVersion: 9, aircraft: Object.fromEntries(devices.map((item) => [item.drone_id, item])),
    selection: devices.map((item) => item.drone_id), armed: true,
    lastStateEvent: { rosterVersion: 9, t: NOW, source: 'console', receivedAt: NOW },
  }
}

function destination(zoneId: string, name: string, overrides: Partial<NavigationDestination> = {}): NavigationDestination {
  return { zoneId, name, aliases: [], floorId: 'floor-1', excluded: false, reachability: 'reachable', allowedClasses: ['aircraft', 'ground_vehicle'], ...overrides }
}

function catalog(overrides: Partial<NavigationCatalog> = {}): NavigationCatalog {
  const parsed = parseNavigationCatalog({
    session: SESSION, catalogVersion: 'catalog-7', receivedAt: NOW - 1000, expiresAt: NOW + 60_000,
    map: {
      mapId: 'accepted-building', floorId: 'floor-1', frame: 'world', accepted: true, approvalId: 'approval-3',
      mapPin: { version: 'map-4', contentSha256: 'a'.repeat(64) },
      geometryPin: { version: 'geometry-5', contentSha256: 'b'.repeat(64) },
      navigationPin: { version: 'navigation-6', contentSha256: 'c'.repeat(64) },
    },
    configVersion: 'motion-8', motionConfig: { test_only_measured_ground_speed_m_s: 0.2 },
    destinations: [
      destination('lobby', 'Main lobby', { aliases: ['Reception'] }),
      destination('lab-east', 'East laboratory', { aliases: ['Lab'] }),
      destination('lab-west', 'West laboratory', { aliases: ['Lab'] }),
      destination('excluded', 'Restricted room', { excluded: true }),
      destination('upstairs', 'Upper room', { floorId: 'floor-2' }),
      destination('closed', 'Closed room', { reachability: 'unreachable' }),
      destination('unknown', 'Unassessed room', { reachability: 'unknown' }),
      destination('air-only', 'Air route', { allowedClasses: ['aircraft'] }),
    ], ...overrides,
  })
  if (!parsed) throw new Error('invalid test catalog')
  return parsed
}

function preview(current: ControlState, accepted = catalog(), refusedId?: number): NavigationPreview {
  const selected = navigationTargets(current)
  const parsed = parseNavigationPreview({
    previewId: 'preview-1', intentId: 'intent-1', session: SESSION, rosterVersion: current.rosterVersion,
    selected, destination: accepted.destinations[0], map: accepted.map,
    catalogVersion: accepted.catalogVersion, configVersion: accepted.configVersion, motionConfig: accepted.motionConfig,
    receivedAt: NOW, expiresAt: NOW + 30_000, dispatchEligible: false,
    routes: selected.filter((target) => target.id !== refusedId).map((target, index) => {
      const end = { xM: index + 2, yM: 3, zM: target.deviceClass === 'aircraft' ? 1.5 : 0, floorId: 'floor-1', frame: 'world' }
      return {
        target,
        waypoints: [{ ...end, xM: 0, yM: 0 }, end],
        arrivalSlot: { slotId: `slot-${target.id}`, zoneId: 'lobby', position: end },
        holdBehavior: target.deviceClass === 'aircraft' ? 'hover' : 'stop',
      }
    }),
    outcomes: selected.map((target) => ({
      target, status: target.id === refusedId ? 'refused' : 'planned',
      code: target.id === refusedId ? 'unsupported_for_device_class' : 'route_planned',
      detail: target.id === refusedId ? 'Navigation is not supported by this selected adapter.' : 'Route and arrival slot reported by the planner.',
    })),
  })
  if (!parsed) throw new Error('invalid test preview')
  return parsed
}

function snapshot(accepted = catalog(), planned: NavigationPreview | null = null): NavigationSnapshot {
  return { status: 'ready', reason: null, catalog: accepted, preview: planned }
}

async function search(value: string) {
  const user = userEvent.setup()
  await user.type(screen.getByRole('searchbox', { name: 'Destination name or alias' }), value)
  return user
}

const reviewButton = () => screen.getByRole('button', { name: 'Review destination' })

describe('named destinations remain a review-only console surface', () => {
  test('the absent production client renders unavailable without creating a catalog or motion action', async () => {
    const onPreview = vi.fn()
    render(<NavigationPane state={state()} snapshot={new UnavailableNavigationClient().getSnapshot()} now={NOW} onPreview={onPreview} />)
    expect(screen.getByText(/no accepted-map catalog or frozen-preview backend is connected/)).toBeInTheDocument()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(reviewButton()).toBeDisabled()
    await userEvent.setup().click(reviewButton())
    expect(onPreview).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /confirm|takeoff|capture|survey|formation/i })).not.toBeInTheDocument()
  })

  test.each<[string, DeviceClass[]]>([
    ['aircraft', ['aircraft']], ['ground', ['ground_vehicle']], ['mixed', ['aircraft', 'ground_vehicle']],
  ])('%s selection uses actual class/unit/epoch and requests only the canonical destination identity', async (_name, classes) => {
    const current = state(classes)
    const onPreview = vi.fn()
    const onDestinationChange = vi.fn()
    render(<NavigationPane state={current} snapshot={snapshot()} now={NOW} onPreview={onPreview} onDestinationChange={onDestinationChange} />)
    const targets = within(screen.getByRole('list', { name: 'Navigation targets' }))
    expect(targets.getAllByRole('listitem')).toHaveLength(classes.length)
    classes.forEach((kind, index) => {
      expect(targets.getByText(`${kind === 'aircraft' ? 'D' : 'G'}-0${index + 3}`)).toBeInTheDocument()
      expect(targets.getByText(new RegExp(`epoch ${index + 7}`))).toBeInTheDocument()
    })
    const user = await search('  RECEPTION')
    expect(reviewButton()).toBeEnabled()
    expect(onPreview).not.toHaveBeenCalled()
    await user.click(reviewButton())
    expect(onPreview).toHaveBeenCalledExactlyOnceWith('lobby')
    expect(onDestinationChange).toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /confirm and send/i })).not.toBeInTheDocument()
  })

  test('an ambiguous alias requires an explicit canonical choice', async () => {
    const onPreview = vi.fn()
    render(<NavigationPane state={state()} snapshot={snapshot()} now={NOW} onPreview={onPreview} />)
    const user = await search('Lab')
    expect(reviewButton()).toBeDisabled()
    expect(screen.getByText(/This name matches more than one destination/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /East laboratory/ }))
    expect(reviewButton()).toBeEnabled()
    expect(onPreview).not.toHaveBeenCalled()
    await user.click(reviewButton())
    expect(onPreview).toHaveBeenCalledExactlyOnceWith('lab-east')
  })

  test('shows a failed review while allowing a new request from a ready catalog', async () => {
    const onPreview = vi.fn()
    const reason = 'The planner response belongs to another request.'
    render(<NavigationPane state={state()} snapshot={{ ...snapshot(), reason }} now={NOW} onPreview={onPreview} />)
    const user = await search('Reception')
    expect(screen.getByRole('status')).toHaveTextContent(reason)
    expect(screen.queryByRole('region', { name: 'Navigation planner preview' })).not.toBeInTheDocument()
    expect(reviewButton()).toBeEnabled()
    await user.click(reviewButton())
    expect(onPreview).toHaveBeenCalledExactlyOnceWith('lobby')
  })

  test.each(['error', 'unavailable'] as const)('a provider %s still prevents retry against its cached catalog', async (status) => {
    const onPreview = vi.fn()
    const reason = 'The destination service is unavailable.'
    render(<NavigationPane state={state()} snapshot={{ ...snapshot(), status, reason }} now={NOW} onPreview={onPreview} />)
    const user = await search('Reception')
    expect(screen.getByRole('status')).toHaveTextContent(reason)
    expect(reviewButton()).toBeDisabled()
    await user.click(reviewButton())
    expect(onPreview).not.toHaveBeenCalled()
  })

  test('an explicit canonical choice is not reinterpreted as another destination’s alias', async () => {
    const accepted = catalog({ destinations: [destination('lobby', 'Main lobby'), destination('other', 'Other hall', { aliases: ['lobby'] })] })
    const onPreview = vi.fn()
    render(<NavigationPane state={state()} snapshot={snapshot(accepted)} now={NOW} onPreview={onPreview} />)
    const user = await search('lobby')
    expect(reviewButton()).toBeDisabled()
    await user.click(screen.getByRole('button', { name: /Main lobby/ }))
    expect(reviewButton()).toBeEnabled()
    await user.click(reviewButton())
    expect(onPreview).toHaveBeenCalledExactlyOnceWith('lobby')
  })

  test.each([
    ['Missing destination', 'No accepted destination matches this name.'],
    ['Restricted room', 'This destination is excluded from navigation.'],
    ['Upper room', 'This destination is on another floor.'],
    ['Closed room', 'This destination is unreachable.'],
    ['Unassessed room', 'Destination reachability has not been established.'],
    ['Air route', 'This destination does not support every selected device class.'],
  ])('refuses %s instead of requesting a different route or subset', async (name, reason) => {
    const onPreview = vi.fn()
    render(<NavigationPane state={state(['ground_vehicle'])} snapshot={snapshot()} now={NOW} onPreview={onPreview} />)
    const user = await search(name)
    expect(screen.getByText(reason)).toBeInTheDocument()
    expect(reviewButton()).toBeDisabled()
    await user.click(reviewButton())
    expect(onPreview).not.toHaveBeenCalled()
  })

  test.each(['capability', 'grounded', 'stale', 'stop', 'disconnected', 'missing_target'] as const)('blocks review when %s evidence is unavailable', async (change) => {
    const current = state()
    if (change === 'capability') current.enabledIntentNames = []
    if (change === 'grounded') current.aircraft[11] = { ...current.aircraft[11], flight_state: 'landed' }
    if (change === 'stale') current.aircraft[11] = { ...current.aircraft[11], last_seen_at: NOW - 5001 }
    if (change === 'stop') current.estop = true
    if (change === 'disconnected') current.connection = { ...current.connection, status: 'disconnected' }
    if (change === 'missing_target') current.selection = [99]
    const onPreview = vi.fn()
    render(<NavigationPane state={current} snapshot={snapshot()} now={NOW} onPreview={onPreview} />)
    const user = await search('Reception')
    expect(reviewButton()).toBeDisabled()
    await user.click(reviewButton())
    expect(onPreview).not.toHaveBeenCalled()
    if (change === 'grounded') expect(screen.getByText(/Takeoff is a separate operation/)).toBeInTheDocument()
  })

  test.each(['session', 'expired'] as const)('does not review a %s catalog', async (change) => {
    const accepted = catalog(change === 'session' ? { session: 'other-session' } : { expiresAt: NOW })
    const onPreview = vi.fn()
    render(<NavigationPane state={state()} snapshot={snapshot(accepted)} now={NOW} onPreview={onPreview} />)
    await search('Reception')
    expect(reviewButton()).toBeDisabled()
    expect(onPreview).not.toHaveBeenCalled()
    expect(screen.getByText(change === 'session' ? 'The catalog belongs to another session.' : 'The accepted destination catalog has expired.')).toBeInTheDocument()
  })
})

describe('frozen planner review display', () => {
  test('shows routes, arrival slots, hold behavior and versioned inputs for a mixed selection', () => {
    const current = state(['aircraft', 'ground_vehicle'])
    render(<NavigationPane state={current} snapshot={snapshot(catalog(), preview(current))} now={NOW} onPreview={vi.fn()} />)
    const details = within(screen.getByRole('region', { name: 'Navigation planner preview' }))
    expect(details.getAllByRole('listitem').some((item) => item.textContent?.includes('Aircraft ID 11 · epoch 7'))).toBe(true)
    expect(details.getByText('Ground robot ID 12 · epoch 8')).toBeInTheDocument()
    expect(details.getByText(/Arrival slot slot-11/)).toBeInTheDocument()
    expect(details.getByText(/Arrival slot slot-12/)).toBeInTheDocument()
    expect(details.getByText('On arrival: hover and hold position.')).toBeInTheDocument()
    expect(details.getByText('On arrival: stop and hold position.')).toBeInTheDocument()
    expect(details.getByText('Map approval')).toBeInTheDocument()
    expect(details.getByText('motion-8')).toBeInTheDocument()
    expect(details.getByText(/"test_only_measured_ground_speed_m_s": 0.2/)).toBeInTheDocument()
    expect(details.getByText(NAVIGATION_CONFIRMATION_UNAVAILABLE)).toBeInTheDocument()
    expect(details.queryByRole('button')).not.toBeInTheDocument()
  })

  test('keeps a current typed per-node refusal visible without dropping its identity', () => {
    const current = state(['aircraft', 'ground_vehicle'])
    render(<NavigationPane state={current} snapshot={snapshot(catalog(), preview(current, catalog(), 12))} now={NOW} onPreview={vi.fn()} />)
    const details = within(screen.getByRole('region', { name: 'Navigation planner preview' }))
    expect(details.getByText('Ground robot ID 12 · epoch 8')).toBeInTheDocument()
    expect(details.getByText(/Refused · unsupported_for_device_class/)).toBeInTheDocument()
    expect(details.queryByText(/Arrival slot slot-12/)).not.toBeInTheDocument()
    expect(details.getByText(/Arrival slot slot-11/)).toBeInTheDocument()
  })

  test.each(['epoch', 'class', 'selection', 'roster', 'map', 'configuration', 'catalog', 'expiry'] as const)('hides the prior route immediately after %s changes', (change) => {
    const current = state(['aircraft', 'ground_vehicle'])
    const accepted = catalog()
    const planned = preview(current, accepted)
    const { rerender } = render(<NavigationPane state={current} snapshot={snapshot(accepted, planned)} now={NOW} onPreview={vi.fn()} />)
    expect(screen.getByRole('region', { name: 'Navigation planner preview' })).toBeInTheDocument()
    const next = { ...current, aircraft: { ...current.aircraft } }
    let nextCatalog = accepted
    if (change === 'epoch') next.aircraft[11] = { ...current.aircraft[11], connection_epoch: 10 }
    if (change === 'class') next.aircraft[11] = { ...current.aircraft[11], device_class: 'ground_vehicle', flight_state: 'idle' }
    if (change === 'selection') next.selection = [11]
    if (change === 'roster') next.rosterVersion = 10
    if (change === 'map') nextCatalog = catalog({ map: { ...accepted.map, geometryPin: { version: 'geometry-new', contentSha256: 'd'.repeat(64) } } })
    if (change === 'configuration') nextCatalog = catalog({ motionConfig: { test_only_measured_ground_speed_m_s: 0.1 } })
    if (change === 'catalog') nextCatalog = catalog({ catalogVersion: 'catalog-new' })
    rerender(<NavigationPane state={next} snapshot={snapshot(nextCatalog, planned)} now={change === 'expiry' ? planned.expiresAt : NOW} onPreview={vi.fn()} />)
    expect(screen.queryByRole('region', { name: 'Navigation planner preview' })).not.toBeInTheDocument()
    expect(screen.getByText(/The previous destination review is no longer current/)).toBeInTheDocument()
  })

  test('editing the destination hides a different planner review and requests invalidation', async () => {
    const current = state()
    const onDestinationChange = vi.fn()
    const onPreview = vi.fn()
    render(<NavigationPane state={current} snapshot={snapshot(catalog(), preview(current))} now={NOW} onPreview={onPreview} onDestinationChange={onDestinationChange} />)
    await search('East laboratory')
    expect(screen.queryByRole('region', { name: 'Navigation planner preview' })).not.toBeInTheDocument()
    expect(onDestinationChange).toHaveBeenCalled()
    expect(onPreview).not.toHaveBeenCalled()
  })

  test('standalone dock details mark expired evidence and never offer execution', () => {
    const planned = preview(state())
    render(<NavigationPreviewDetails preview={planned} now={planned.expiresAt} />)
    expect(screen.getByText('Preview expired. Request a new destination review.')).toBeInTheDocument()
    expect(screen.getByText(NAVIGATION_CONFIRMATION_UNAVAILABLE)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
