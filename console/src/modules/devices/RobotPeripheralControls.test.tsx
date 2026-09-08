import { act, fireEvent, render, screen } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, test } from 'vitest'
import { useControlConsole } from '../../control/use-control-console'
import { C1_BASIC_CONTROL_INTENTS, isRobotPeripheralArgs, type RelayAircraftState, type RelayStateEvent } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import { publicNodeEvents } from '../../testing/public-node-events'
import { RobotPeripheralControls } from './RobotPeripheralControls'

const session = 'robot-peripherals-test'
const t = 1788726306375

async function mount() {
  let sequence = 0
  let control!: ReturnType<typeof useControlConsole>
  const clients = { console: new FixtureRelayClient(session, () => t, 'console'), keyboard: new FixtureRelayClient(session, () => t, 'keyboard') }
  const intentDependencies = { now: () => t, nextId: () => `peripheral-${++sequence}` }
  function Harness() {
    const current = useControlConsole({ sessionId: session, clients, intentDependencies })
    useEffect(() => { control = current })
    const device = current.state.aircraft[11]
    return device ? <RobotPeripheralControls controller={current} device={device} /> : null
  }
  render(<Harness />)
  await act(async () => {})
  const [, node] = publicNodeEvents(session, 1)
  const device: RelayAircraftState = {
    ...fixtureAircraft(t)[0], drone_id: 11, device_class: 'ground_vehicle', unit: 1,
    membership: 'degraded', selectable: false, control_authority: false, readiness_reasons: ['control_authority_missing'],
    adapter_capabilities: ['robot_peripheral_v1', 'neck', 'speech', 'lights', 'screen'],
    node_status: { ...node, t, drone_id: 11, control_authority: false, device_telemetry: { safety: { blocked: true, reasons: ['lidar_stale'], motion_enabled: false } } },
  }
  const emit = (patch: Partial<RelayAircraftState> = {}) => act(() => clients.console.emitServer({
    v: 1, t, type: 'state', event_id: `state-${++sequence}`, session, roster_version: 7,
    armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'robot_peripheral'],
    pending: null, accepted_plan: null, drones: [{ ...device, ...patch }],
  } satisfies RelayStateEvent))
  emit()
  fireEvent.click(screen.getByText('Robot peripheral controls · G-01'))
  return { get: () => control, clients, emit, device }
}

describe('robot peripheral controls', () => {
  test('stationary controls preview one degraded robot without arming or changing fleet selection', async () => {
    const rig = await mount()
    expect(screen.getByRole('button', { name: 'Preview neck tilt' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Preview base lights' })).toBeEnabled()
    fireEvent.change(screen.getByRole('textbox', { name: 'Text to speak · up to 240 characters' }), { target: { value: 'Please keep the path clear.' } })
    expect(rig.clients.console.sent).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Preview speech' }))
    const draft = rig.get().pendingRequest!.intent
    expect(draft).toMatchObject({ name: 'robot_peripheral', args: { kind: 'speech', text: 'Please keep the path clear.' }, selection: [11], confirm: false })
    expect(rig.get().state.selection).toEqual([])
    expect(rig.get().state.armed).toBe(false)
    expect(rig.clients.console.sent).toEqual([])
    await act(async () => { rig.get().confirmRequest(draft.intent_id) })
    expect(rig.clients.console.sent).toEqual([{ ...draft, confirm: true }])
  })
  test.each([{ connection_epoch: 2 }, { membership: 'disconnected' as const }, { adapter_capabilities: ['robot_peripheral_v1'] }])('changed target eligibility invalidates exact preview: %j', async (change) => {
    const rig = await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Preview screen message' }))
    const id = rig.get().pendingRequest!.intent.intent_id
    rig.emit(change)
    await act(async () => { rig.get().confirmRequest(id) })
    expect(rig.clients.console.sent).toEqual([])
  })
  test.each(['missing', 'stale', 'future', 'hold', 'failsafe'] as const)('all controls require a current nominal node lease: %s', async (condition) => {
    const rig = await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Preview screen message' }))
    const id = rig.get().pendingRequest!.intent.intent_id
    const status = rig.device.node_status!
    rig.emit({ node_status: condition === 'missing' ? null : {
      ...status,
      t: condition === 'stale' ? t - 5001 : condition === 'future' ? t + 1 : t,
      watchdog_state: condition === 'hold' || condition === 'failsafe' ? condition : 'nominal',
    } })
    for (const name of ['Preview neck tilt', 'Preview base lights', 'Preview speech', 'Preview screen message']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
    await act(async () => { rig.get().confirmRequest(id) })
    expect(rig.clients.console.sent).toEqual([])
  })
  test('neck authority comes from the current node report and is rechecked at confirmation', async () => {
    const rig = await mount()
    const status = { ...rig.device.node_status!, device_telemetry: { safety: { motion_enabled: true } } }
    rig.emit({ node_status: { ...status, control_authority: false } })
    expect(screen.getByRole('button', { name: 'Preview neck tilt' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Preview screen message' })).toBeEnabled()
    rig.emit({ node_status: { ...status, control_authority: true } })
    expect(screen.getByRole('button', { name: 'Preview neck tilt' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Preview neck tilt' }))
    const id = rig.get().pendingRequest!.intent.intent_id
    rig.emit({ node_status: { ...status, control_authority: false } })
    await act(async () => { rig.get().confirmRequest(id) })
    expect(rig.clients.console.sent).toEqual([])
    expect(rig.get().state.armed).toBe(false)
    expect(rig.get().state.selection).toEqual([])
  })
  test('unsupported controls remain visible and disabled; unsafe text cannot be previewed', async () => {
    const rig = await mount()
    rig.emit({ adapter_capabilities: ['robot_peripheral_v1', 'speech'] })
    expect(screen.getByRole('button', { name: 'Preview base lights' })).toBeDisabled()
    expect(screen.getByText('lights is not supported by this connected adapter.')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'Text to speak · up to 240 characters' }), { target: { value: 'first\nsecond' } })
    expect(screen.getByRole('button', { name: 'Preview speech' })).toBeDisabled()
    expect(rig.clients.console.sent).toEqual([])
  })
  test.each([{}, { kind: [] }, { kind: 'neck', position: 299 }, { kind: 'neck', position: true },
    { kind: 'lights', h: 0, s: 0, v: 256 }, { kind: 'speech', text: '' }, { kind: 'speech', text: 'a\nb' },
    { kind: 'screen', text: 'x'.repeat(241) }, { kind: 'screen', text: 'a\u00a0b' }, { kind: 'screen', text: '\ue000' },
    { kind: 'screen', text: 'x', html: true }])('rejects arguments outside the node contract: %j', (args) => expect(isRobotPeripheralArgs(args)).toBe(false))
  test('accepts exact endpoints, Unicode printable text and screen clearing', () => {
    for (const args of [{ kind: 'neck', position: 300 }, { kind: 'neck', position: 650 },
      { kind: 'lights', h: 0, s: 255, v: 0 }, { kind: 'speech', text: 'hello world 🚗' },
      { kind: 'screen', text: '' }]) expect(isRobotPeripheralArgs(args)).toBe(true)
  })
})
