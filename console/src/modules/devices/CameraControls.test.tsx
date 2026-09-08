import { act, fireEvent, render, screen } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, test } from 'vitest'
import { cameraControlBlockedReason } from '../../control/camera'
import { useControlConsole } from '../../control/use-control-console'
import { C1_BASIC_CONTROL_INTENTS, isCameraControlArgs, isConsoleIntentV1, type RelayAircraftState, type RelayStateEvent } from '../../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../../testing/fixture-relay-client'
import { publicNodeEvents } from '../../testing/public-node-events'
import { CameraControls } from './CameraControls'

const session = 'camera-controls-test'
const t = 1788726306375

async function mount() {
  let now = t
  let sequence = 0
  let control!: ReturnType<typeof useControlConsole>
  const clients = { console: new FixtureRelayClient(session, () => now, 'console'), keyboard: new FixtureRelayClient(session, () => now, 'keyboard') }
  const intentDependencies = { now: () => now, nextId: () => `camera-${++sequence}` }
  function Harness() {
    const current = useControlConsole({ sessionId: session, clients, intentDependencies })
    useEffect(() => { control = current })
    const device = current.state.aircraft[1]
    return device ? <CameraControls controller={current} device={device} now={now} /> : null
  }
  render(<Harness />)
  await act(async () => {})
  const [, node] = publicNodeEvents(session, 1)
  const device: RelayAircraftState = {
    ...fixtureAircraft(now)[0], membership: 'degraded', selectable: false, control_authority: true,
    readiness_reasons: ['home_pose_unconfirmed'], connection_epoch: 1,
    adapter_capabilities: ['flight', 'camera_control_v1'],
    node_status: { ...node, t: now, drone_id: 1, control_authority: true, watchdog_state: 'nominal', device_telemetry: {
      controls: { supported_operations: ['camera_ready', 'capture_photo', 'set_gimbal_pitch'] },
      gimbal: { pitch_deg: 0, pitch_min_deg: -90, pitch_max_deg: 20, connected: true },
      camera: { mode: 'PHOTO_NORMAL', busy: false, recording: false },
    } },
  }
  const emit = (patch: Partial<RelayAircraftState> = {}) => act(() => clients.console.emitServer({
    v: 1, t: now, type: 'state', event_id: `state-${++sequence}`, session,
    roster_version: 7, armed: false, estop: false, selection: [], formation: 'none', spacing: 0.8, mode: 'indoor',
    capability_profile: 'c1_basic_control', enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS, 'camera_control'],
    pending: null, accepted_plan: null, drones: [{ ...device, ...patch }],
  } satisfies RelayStateEvent))
  emit()
  fireEvent.click(screen.getByText('Camera controls · D-01'))
  return { get: () => control, clients, emit, device, advance: (ms: number) => { now += ms } }
}

describe('standalone camera controls', () => {
  test('single photo targets the connected aircraft independently, then requires exact confirmation', async () => {
    const rig = await mount()
    expect(screen.getByRole('button', { name: 'Preview single photo' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Preview single photo' }))
    const draft = rig.get().pendingRequest!.intent
    expect(draft).toMatchObject({ name: 'camera_control', args: { kind: 'photo' }, selection: [1], confirm: false })
    expect(rig.get().state.selection).toEqual([])
    expect(rig.clients.console.sent).toEqual([])
    await act(async () => { rig.get().confirmRequest(draft.intent_id) })
    expect(rig.clients.console.sent).toHaveLength(1)
    expect(rig.clients.console.sent[0]).toMatchObject({ ...draft, confirm: true })
    expect(isConsoleIntentV1(rig.clients.console.sent[0])).toBe(true)
  })

  test('retrying a refused camera action requires a new preview and confirmation', async () => {
    const rig = await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Preview single photo' }))
    const draft = rig.get().pendingRequest!.intent
    await act(async () => { rig.get().confirmRequest(draft.intent_id) })
    const refused = rig.get().state.requests.find((request) => request.intent.intent_id === draft.intent_id)!
    expect(refused.status).toBe('refused') // The test relay does not execute camera commands.
    act(() => rig.get().retryRequest(refused))
    expect(rig.clients.console.sent).toHaveLength(1)
    expect(rig.get().pendingRequest?.intent).toMatchObject({ name: 'camera_control', retry_of: draft.intent_id, confirm: false })
  })

  test('a rejoin invalidates a pending camera preview even at the same roster version', async () => {
    const rig = await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Preview photo mode' }))
    const id = rig.get().pendingRequest!.intent.intent_id
    rig.emit({ connection_epoch: 2 })
    await act(async () => { rig.get().confirmRequest(id) })
    expect(rig.clients.console.sent).toEqual([])
    expect(rig.get().state.requests.find((r) => r.intent.intent_id === id)?.status).toBe('invalidated')
  })

  test('fresh measured range bounds gimbal commands and telemetry expiry prevents confirmation', async () => {
    const rig = await mount()
    fireEvent.change(screen.getByLabelText('Absolute pitch · degrees'), { target: { value: '-91' } })
    expect(screen.getByRole('button', { name: 'Preview gimbal pitch' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Absolute pitch · degrees'), { target: { value: '-45' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview gimbal pitch' }))
    const draft = rig.get().pendingRequest!.intent
    expect(draft.args).toEqual({ kind: 'gimbal', pitch_mdeg: -45000 })
    rig.advance(5001)
    await act(async () => { rig.get().confirmRequest(draft.intent_id) })
    expect(rig.clients.console.sent).toEqual([])
  })

  test('missing support, revoked authority and other device classes cannot issue camera controls', async () => {
    const rig = await mount()
    for (const patch of [{ adapter_capabilities: ['flight'] }, { control_authority: false }, { device_class: 'ground_vehicle' as const }]) {
      expect(cameraControlBlockedReason(rig.get().state, { ...rig.device, ...patch }, { kind: 'photo' }, t)).not.toBeNull()
    }
    for (const args of [{ kind: 'photo', capture_id: 'injected' }, { kind: 'gimbal', pitch_mdeg: 0.5 }, { kind: 'gimbal', pitch_mdeg: 180001 }, { kind: 'download' }]) {
      expect(isCameraControlArgs(args)).toBe(false)
    }
  })
})
