import { SwarmPane } from '../modules/control/SwarmPane'
import { act, fireEvent, renderHook, render, screen } from '@testing-library/react'
import { describe, expect, test } from 'vitest'
import { FixtureRelayClient } from '../testing/fixture-relay-client'
import { fieldGroundPose, fieldGroundState } from '../testing/field-ground'
import { isReady } from '../shell/derive'
import { isConsoleIntentV1, parseRelayServerEvent, type IntentV1 } from '../relay/contract'
import { useControlConsole } from './use-control-console'
import { groundPulseArgs } from './ground'
import { GroundPane } from '../modules/control/GroundPane'
import { compileUtterance } from '../speech/compiler'

const T = 1788790000000
const SESSION = 'ground-control-test'
function mount() {
  let now = T
  let id = 0
  const clients = { console: new FixtureRelayClient(SESSION, () => now), keyboard: new FixtureRelayClient(SESSION, () => now, 'keyboard'), webcam: new FixtureRelayClient(SESSION, () => now, 'webcam') }
  const hook = renderHook(() => useControlConsole({ sessionId: SESSION, clients, intentDependencies: { now: () => now, nextId: () => `ground-${++id}` } }))
  act(() => { clients.console.emitServer(fieldGroundState(T, SESSION)) })
  const ready = () => act(() => { clients.console.emitServer(fieldGroundPose(T + 1, SESSION)); clients.console.emitServer(fieldGroundState(T + 2, SESSION)) })
  return { ...hook, clients, ready, advance: (ms: number) => { now += ms } }
}

describe('confirmed canonical ground controls', () => {
  test('requires accepted positive-confidence declared-source pose followed by authoritative ready state', () => {
    const { result, clients } = mount()
    expect(isReady(result.current.state.aircraft[11])).toBe(false)
    act(() => { clients.console.emitServer(fieldGroundPose(T + 1, SESSION)) })
    expect(isReady(result.current.state.aircraft[11])).toBe(false)
    act(() => { clients.console.emitServer(fieldGroundState(T + 2, SESSION)) })
    expect(isReady(result.current.state.aircraft[11])).toBe(true)
    expect(result.current.state.aircraft[11].telemetry).toBeNull()
    expect(result.current.state.aircraft[11].client_observation?.ground?.pose?.frame).toBe('odom')
    expect(clients.console.sent).toEqual([])
  })

  test.each(['console', 'webcam'] as const)('confirms exact %s source once and requires a fresh retry preview', (source) => {
    const { result, clients, ready } = mount(); ready()
    act(() => { result.current.prepareIntent({ name: 'ground_velocity', args: groundPulseArgs('left') }, source) })
    const preview = result.current.pendingRequest!
    expect(preview.plan?.groundSources).toEqual({ 11: 'isolated-pose' })
    expect(clients[source].sent).toEqual([])
    act(() => { result.current.confirmRequest(preview.intent.intent_id); result.current.confirmRequest(preview.intent.intent_id) })
    expect(clients[source].sent).toHaveLength(1)
    expect(clients[source].sent[0]).toMatchObject({ name: 'ground_velocity', source, selection: [11], confirm: true, args: groundPulseArgs('left') })
    const refused = { ...result.current.state.requests[0], status: 'refused' as const }
    act(() => { result.current.retryRequest(refused) })
    expect(clients[source].sent).toHaveLength(1)
    expect(result.current.pendingRequest?.intent).toMatchObject({ confirm: false, retry_of: preview.intent.intent_id })
    expect(clients.keyboard.sent).toEqual([])
  })

  test.each(['source', 'unit', 'epoch', 'authority', 'selection', 'estop', 'capability', 'time'] as const)('retires a preview when %s changes', (change) => {
    const { result, clients, ready, advance } = mount(); ready()
    act(() => { result.current.prepareIntent({ name: 'ground_velocity', args: groundPulseArgs('forward') }) })
    const id = result.current.pendingRequest!.intent.intent_id
    const event = fieldGroundState(T + 3, SESSION)
    if (change === 'unit') event.drones[0].unit = 2
    if (change === 'source') event.drones[0].ground_readiness = { source_id: 'replacement' }
    if (change === 'epoch') event.drones[0].connection_epoch = 4
    if (change === 'authority') event.drones[0].control_authority = false
    if (change === 'selection') event.selection = []
    if (change === 'estop') event.estop = true
    if (change === 'capability') event.enabled_intent_names = event.enabled_intent_names.filter((name) => name !== 'ground_velocity')
    if (change === 'time') advance(6000)
    else act(() => { clients.console.emitServer(event) })
    act(() => { result.current.confirmRequest(id) })
    expect(clients.console.sent).toEqual([])
    expect(result.current.state.requests[0].status).toBe('invalidated')
  })

  test('ground return stages even direct presses and its retry cannot reuse confirmation', () => {
    const { result, clients, ready } = mount(); ready()
    act(() => { result.current.issueIntent({ name: 'come_home', args: {} }) })
    expect(result.current.pendingRequest?.intent).toMatchObject({ name: 'come_home', confirm: false })
    expect(clients.console.sent).toEqual([])
    act(() => { result.current.confirmRequest(result.current.pendingRequest!.intent.intent_id) })
    expect(clients.console.sent[0]).toMatchObject({ name: 'come_home', selection: [11], args: {}, confirm: true })
  })

  test('manual Ground pane offers bounded previews, retaining configured G-01 label and wire ID11', () => {
    const { result, clients, ready } = mount(); ready()
    render(<GroundPane controller={result.current} />)
    expect(screen.getByText(/G-01 · wire ID 11 · connection epoch 3/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Forward pulse/ }))
    expect(result.current.pendingRequest?.intent.args).toEqual(groundPulseArgs('forward'))
    expect(clients.console.sent).toEqual([])
  })

  test('cannot substitute ground pulses for world translation, aircraft body pulses or mixed targets', () => {
    const { result, clients, ready } = mount(); ready()
    act(() => {
      expect(result.current.prepareIntent({ name: 'translate', args: { dx: 1, dy: 0 } })).toBeNull()
      expect(result.current.prepareIntent({ name: 'ground_velocity', args: groundPulseArgs('forward'), targets: [11, 12] })).toBeNull()
    })
    expect(clients.console.sent).toEqual([])
  })
})

describe('ground language and wire bounds', () => {
  const context = { roomId: 'room-01', pattern: 'pano_360' as const, readyIds: [11], selectedGroundIds: [11], selection: [11] }
  test.each(['forward', 'left', 'right'] as const)('typed pulse %s names exact requested parameters', (direction) => {
    expect(compileUtterance(`pulse ${direction}`, context)).toMatchObject({ status: 'compiled', intent: 'ground_velocity', args: groundPulseArgs(direction) })
  })
  test.each(['move forward two metres', 'turn left 90 degrees', 'pulse forward 2 metres', 'do not pulse forward', 'move G-01 to the kitchen'])('refuses unrepresented phrase %s', (phrase) => {
    expect(compileUtterance(phrase, context).status).toBe('refused')
  })
  test('requires one actual ready ground selection and does not invent return identifiers', () => {
    expect(compileUtterance('pulse forward', { ...context, selection: [11, 12] }).status).toBe('refused')
    expect(compileUtterance('return home', context)).toMatchObject({ intent: 'come_home', args: {} })
  })
  test('wire rejects reverse, combined axes, no-op, excess duration and unconfirmed ground velocity', () => {
    const value: IntentV1 = { v: 1, t: T, type: 'intent', intent_id: 'bounded', retry_of: null, source: 'webcam', session: SESSION, name: 'ground_velocity', args: groundPulseArgs('forward'), selection: [11], mode: 'indoor', confirm: true }
    expect(isConsoleIntentV1(value)).toBe(true)
    for (const args of [{ linear_mm_s: -1, angular_mrad_s: 0, duration_ms: 1 }, { linear_mm_s: 1, angular_mrad_s: 1, duration_ms: 1 }, { linear_mm_s: 0, angular_mrad_s: 0, duration_ms: 1 }, { ...groundPulseArgs('forward'), duration_ms: 501 }]) expect(isConsoleIntentV1({ ...value, args })).toBe(false)
    expect(isConsoleIntentV1({ ...value, confirm: false })).toBe(false)
  })
  test('supervised profile flag is typed and local height stays bounded without becoming world telemetry', () => {
    const frame = fieldGroundState(T, SESSION)
    expect(parseRelayServerEvent({ ...frame, requires_home_pose: false })).not.toBeNull()
    expect(parseRelayServerEvent({ ...frame, requires_home_pose: 'false' })).toBeNull()
  })
})


test('public signed local height accepts only the exact bounded SDK schema', () => {
  const status = { v: 1, type: 'node_status', t: T, event_id: 'height-state', session: SESSION, drone_id: 1, connection_epoch: 2,
    virtual_stick_enabled: false, control_authority: false, authority_change_reason: null, watchdog_state: 'nominal', video_publish_state: 'stopped',
    phone_battery_percent: 80, phone_thermal_state: 'none', local_height: { z_m: 0.2, source: 'flight_controller_altitude', age_ms: 20, reported_at_ms: T } }
  expect(parseRelayServerEvent(status)).toMatchObject({ local_height: status.local_height })
  expect(parseRelayServerEvent({ ...status, local_height: null })).not.toBeNull()
  for (const patch of [{ z_m: NaN }, { source: 'guessed' }, { age_ms: -1 }, { reported_at_ms: null }, { extra: true }]) {
    expect(parseRelayServerEvent({ ...status, local_height: { ...status.local_height, ...patch } })).toBeNull()
  }
})


test('Swarm enables session ARM with selected ground but offers no ground translation or formation', () => {
  const { result, clients } = mount()
  render(<SwarmPane controller={result.current} steps={1} onSteps={() => {}} formationPreview={null} onFormationPreview={() => {}} />)
  const arm = screen.getByRole('button', { name: 'Arm' })
  expect(arm).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Translate north' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'line' })).toBeDisabled()
  expect(screen.queryByRole('region', { name: 'Robot formation preview' })).not.toBeInTheDocument()
  expect(screen.queryByText(/Robots form on the floor|Robots: room east|Robot steps resolve/)).not.toBeInTheDocument()
  fireEvent.click(arm)
  expect(clients.console.sent).toHaveLength(1)
  expect(clients.console.sent[0]).toMatchObject({ name: 'arm', source: 'console', selection: [], args: {} })
})
