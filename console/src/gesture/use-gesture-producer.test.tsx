import { fieldGroundPose, fieldGroundState } from '../testing/field-ground'
import { act, render } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, test } from 'vitest'
import { createInitialControlState, type ConnectionStatus } from '../control/state'
import { useControlConsole, type ControlClients } from '../control/use-control-console'
import { isConsoleIntentV1 } from '../relay/contract'
import { FixtureRelayClient } from '../testing/fixture-relay-client'
import { createGestureTestRig, type GestureTestRig } from '../testing/gesture-fixtures'
import type { GestureCategory, GestureProfile } from './policy'
import { emissionBlockedReason, useGestureProducer } from './use-gesture-producer'

const session = 'gesture-hook-session'

type Latest = {
  control: ReturnType<typeof useControlConsole>
  producer: ReturnType<typeof useGestureProducer>
}

function Harness({
  clients,
  rig,
  roomId,
  onRender,
  profile,
}: {
  clients: ControlClients
  profile?: GestureProfile
  rig: GestureTestRig
  roomId: string
  onRender: (latest: Latest) => void
}) {
  let sequence = 0
  const control = useControlConsole({
    sessionId: session,
    clients,
    intentDependencies: {
      now: () => rig.dependencies.clock.wall(),
      nextId: () => `gesture-intent-${++sequence}`,
    },
  })
  const producer = useGestureProducer({ control, roomId, dependencies: rig.dependencies, profile })
  const { videoRef } = producer
  useEffect(() => {
    onRender({ control, producer })
  })
  return <video ref={videoRef} />
}

interface FixtureClients extends ControlClients {
  console: FixtureRelayClient
  keyboard: FixtureRelayClient
  webcam?: FixtureRelayClient
}

function fixtureClients(rig: GestureTestRig, withWebcam = true, mixed = false, c2 = false): FixtureClients {
  const wall = () => rig.dependencies.clock.wall()
  return {
    console: new FixtureRelayClient(session, wall, 'console', mixed ? 'mixed' : 4, true, c2 ? 'c2_fleet_operations' : 'c1_basic_control'),
    keyboard: new FixtureRelayClient(session, wall, 'keyboard', mixed ? 'mixed' : 4, true, c2 ? 'c2_fleet_operations' : 'c1_basic_control'),
    ...(withWebcam ? { webcam: new FixtureRelayClient(session, wall, 'webcam', mixed ? 'mixed' : 4, true, c2 ? 'c2_fleet_operations' : 'c1_basic_control') } : {}),
  }
}

async function mount(options: { loadError?: Error; withWebcam?: boolean; roomId?: string; profile?: GestureProfile; mixed?: boolean; c2?: boolean } = {}) {
  const rig = createGestureTestRig({ loadError: options.loadError })
  const clients = fixtureClients(rig, options.withWebcam ?? true, options.mixed, options.c2)
  const latest: { current: Latest | null } = { current: null }
  render(
    <Harness
      clients={clients}
      rig={rig}
      roomId={options.roomId ?? 'room-01'}
      profile={options.profile}
      onRender={(value) => {
        latest.current = value
      }}
    />,
  )
  await act(async () => {})
  const get = () => latest.current as Latest
  const hold = (category: GestureCategory | null, durationMs: number, score = 0.95) => {
    for (let elapsed = 0; elapsed < durationMs; elapsed += 50) {
      act(() => rig.frame(category === null ? [] : [{ ...hand(category, score) }], 50))
    }
  }
  const enable = async () => {
    await act(async () => {
      await get().producer.enable()
    })
  }
  return { rig, clients, get, hold, enable }
}

function hand(category: GestureCategory, score: number) {
  return {
    category,
    rawCategory: category,
    score,
    handedness: 'Right',
    landmarks: Array.from({ length: 21 }, (_, index) => ({ x: index / 21, y: 0.5, z: 0 })),
  }
}

describe('useGestureProducer', () => {
  test('tracking is off by default and touches neither the camera nor the model until enabled', async () => {
    const { rig, get } = await mount()
    expect(get().control.state.webcamConnection.status).toBe('connected')
    expect(get().producer.view.status).toBe('disabled')
    expect(get().producer.view.enabled).toBe(false)
    expect(rig.camera.startCalls).toBe(0)
    expect(rig.source.loadCalls).toBe(0)
    expect(rig.scheduler.pending).toBe(false)
  })

  test('an open palm drafts a webcam capture_room preview and a thumb up confirms the same intent_id', async () => {
    const { rig, clients, get, hold, enable } = await mount()
    await enable()
    expect(get().producer.view.status).toBe('tracking')
    expect(rig.camera.startCalls).toBe(1)
    expect(rig.source.loadCalls).toBe(1)

    hold('Open_Palm', 650)
    const pending = get().control.pendingRequest
    expect(pending).not.toBeNull()
    expect(pending?.intent).toMatchObject({
      name: 'capture_room',
      source: 'webcam',
      confirm: false,
      selection: [1],
      args: { room_id: 'room-01', pattern: 'pano_360', capture_id: `capture-${pending?.intent.intent_id}` },
    })
    expect(pending?.status).toBe('pending_confirmation')
    expect(pending?.plan?.title).toBe('Capture room')
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({ kind: 'draft', intentId: pending?.intent.intent_id })

    hold(null, 250)
    expect(get().producer.view.phase).toBe('idle')

    hold('Thumb_Up', 450)
    const sent = clients.webcam?.sent ?? []
    expect(sent).toHaveLength(1)
    expect(sent[0]).toMatchObject({
      intent_id: pending?.intent.intent_id,
      name: 'capture_room',
      source: 'webcam',
      confirm: true,
      t: rig.dependencies.clock.wall(),
    })
    expect(isConsoleIntentV1(sent[0])).toBe(true)
    expect(clients.console.sent).toHaveLength(0)
    expect(get().control.state.requests[0]).toMatchObject({
      status: 'accepted',
      intent: { intent_id: pending?.intent.intent_id },
    })
    expect(get().control.pendingRequest).toBeNull()
    expect(get().producer.view.lastAction).toMatchObject({ kind: 'confirm', intentId: pending?.intent.intent_id })
  })

  test('a closed fist drafts a previewed hold that must be confirmed before it is sent', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()

    hold('Closed_Fist', 650)
    const pending = get().control.pendingRequest
    expect(pending?.intent).toMatchObject({ name: 'hold', source: 'webcam', confirm: false, args: {}, selection: [1] })
    expect(pending?.plan?.title).toBe('hold')
    expect(clients.webcam?.sent).toHaveLength(0)

    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({
      intent_id: pending?.intent.intent_id,
      name: 'hold',
      source: 'webcam',
      confirm: true,
    })
    expect(isConsoleIntentV1(clients.webcam?.sent[0])).toBe(true)
  })

  test('a thumb down cancels the pending gesture preview and nothing is sent', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 650)
    const intentId = get().control.pendingRequest?.intent.intent_id
    hold(null, 250)
    hold('Thumb_Down', 450)

    expect(get().control.pendingRequest).toBeNull()
    expect(get().control.state.requests[0]).toMatchObject({ status: 'cancelled', intent: { intent_id: intentId } })
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({ kind: 'cancel', intentId })
  })

  test('low confidence emits nothing and is shown', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 1000, 0.59)
    expect(get().control.pendingRequest).toBeNull()
    expect(get().control.state.requests).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(get().producer.view.notable?.outcome).toMatchObject({ kind: 'low_confidence', score: 0.59 })
    expect(get().producer.view.phase).toBe('idle')
  })

  test('per-action readiness reports exact targets and an invalid capture room does not block HOLD', async () => {
    const { clients, get, hold, enable } = await mount({ roomId: 'INVALID ROOM' })
    await enable()
    const readiness = get().producer.view.actionReadiness
    expect(readiness.find((item) => item.pair.gesture === 'Open_Palm')).toMatchObject({
      targets: [1], scope: 'devices', blockedReason: 'Enter a valid room identifier (3–24 lowercase letters, digits or hyphens).',
    })
    expect(readiness.find((item) => item.pair.gesture === 'Closed_Fist')).toMatchObject({
      targets: [1], scope: 'devices', blockedReason: null,
    })
    hold('Closed_Fist', 650)
    expect(get().control.pendingRequest?.intent).toMatchObject({ name: 'hold', selection: [1], confirm: false })
    expect(get().producer.view.actionReadiness.find((item) => item.pair.gesture === 'Thumb_Up')).toMatchObject({
      targets: [1], scope: 'devices', blockedReason: null,
    })
    expect(clients.webcam?.sent).toEqual([])
  })

  test('a dwell timeout emits nothing and is shown', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 300)
    hold(null, 100)
    expect(get().control.state.requests).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(get().producer.view.notable?.outcome).toMatchObject({ kind: 'dwell_timeout', reason: 'released' })
  })

  test('a held gesture drafts once and is suppressed until the hand releases', async () => {
    const { get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 1500)
    expect(get().control.state.requests).toHaveLength(1)
    expect(get().producer.view.phase).toBe('wait_for_release')
    expect(get().producer.view.notable?.outcome).toMatchObject({ kind: 'duplicate_suppressed', category: 'Open_Palm' })

    hold(null, 250)
    hold('Closed_Fist', 650)
    expect(get().control.state.requests).toHaveLength(1)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'blocked',
      detail: 'A plan preview is already pending; confirm or cancel it before drafting another.',
    })
  })

  test('confirm and cancel gestures act only on a gesture-drafted preview', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()

    hold('Thumb_Up', 450)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'blocked',
      detail: 'No plan preview is pending; there is nothing to confirm.',
    })
    hold(null, 250)

    act(() => {
      get().control.prepareCapture('room-01', 'console')
    })
    expect(get().control.pendingRequest?.intent.source).toBe('console')
    hold('Thumb_Up', 450)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'blocked',
      detail: 'The pending preview was drafted by console; gestures only confirm gesture-drafted previews.',
    })
    expect(get().control.pendingRequest?.status).toBe('pending_confirmation')
    expect(clients.console.sent).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
  })

  test('a dropped webcam disables emission and stops the frame loop', async () => {
    const { rig, clients, get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 300)
    const recognizedBefore = rig.source.recognized.length

    act(() => rig.camera.dropWebcam())
    expect(get().producer.view.status).toBe('webcam_dropped')
    expect(get().producer.view.statusDetail).toContain('unplugged')
    expect(get().producer.view.emissionBlockedReason).toContain('unplugged')
    expect(get().producer.view.phase).toBe('idle')

    hold('Open_Palm', 1000)
    expect(rig.source.recognized).toHaveLength(recognizedBefore)
    expect(get().control.state.requests).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
  })

  test('a model that fails to load disables emission and releases the camera', async () => {
    const { rig, clients, get, hold, enable } = await mount({ loadError: new Error('cdn unreachable') })
    await enable()
    expect(get().producer.view.status).toBe('model_failed_to_load')
    expect(get().producer.view.recognizer).toBe('model_failed_to_load')
    expect(get().producer.view.recognizerDetail).toBe('cdn unreachable')
    expect(get().producer.view.enabled).toBe(true)
    expect(rig.camera.controller.state.status).toBe('idle')
    expect(rig.source.closed).toBe(true)

    hold('Open_Palm', 1000)
    expect(rig.source.recognized).toHaveLength(0)
    expect(get().control.state.requests).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
  })

  test('a denied camera permission is a distinct state that emits nothing', async () => {
    const { rig, get, hold, enable } = await mount()
    rig.camera.denyPermission()
    await enable()
    expect(get().producer.view.status).toBe('permission_denied')
    expect(get().producer.view.statusDetail).toContain('Camera permission was denied')
    expect(rig.source.loadCalls).toBe(0)
    hold('Open_Palm', 1000)
    expect(get().control.state.requests).toHaveLength(0)
  })

  test('without a webcam relay source every accepted gesture is blocked', async () => {
    const { get, hold, enable } = await mount({ withWebcam: false })
    await enable()
    expect(get().control.state.webcamConnection.status).toBe('disconnected')
    hold('Open_Palm', 650)
    expect(get().control.state.requests).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'blocked',
      detail: 'The webcam relay source is not connected; no gesture intent can be sent.',
    })
  })

  test('disable stops the camera, closes the recognizer, and resets the readout', async () => {
    const { rig, get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 300)
    act(() => get().producer.disable())
    expect(get().producer.view.status).toBe('disabled')
    expect(rig.camera.controller.state.status).toBe('idle')
    expect(rig.source.closed).toBe(true)
    expect(get().producer.view.frame).toBeNull()
    expect(rig.scheduler.pending).toBe(false)
  })

  test('the session recording carries recognizer frames, policy transitions, and the intent lifecycle', async () => {
    const { rig, get, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 650)
    const intentId = get().control.pendingRequest?.intent.intent_id
    hold(null, 250)
    hold('Thumb_Up', 450)

    act(() => get().producer.downloadRecording())
    expect(rig.downloads).toHaveLength(1)
    expect(rig.downloads[0].name).toBe(`gesture-session-${session}.jsonl`)
    const lines = rig.downloads[0].contents.trimEnd().split('\n').map((line) => JSON.parse(line))
    expect(lines[0]).toMatchObject({ kind: 'header', v: 1, session, source: 'webcam' })
    expect(lines[0].pairs).toHaveLength(4)
    const kinds = new Set(lines.slice(1).map((line) => line.kind))
    expect(kinds).toEqual(new Set(['status', 'recognizer', 'policy', 'intent']))
    const intents = lines.filter((line) => line.kind === 'intent')
    expect(intents.map((line) => [line.event, line.intent_id])).toEqual([
      ['draft', intentId],
      ['confirm', intentId],
    ])
    expect(intents[0].intent).toMatchObject({ confirm: false, source: 'webcam' })
    expect(intents[1].intent).toMatchObject({ confirm: true, source: 'webcam' })
    const accepted = lines.filter((line) => line.kind === 'policy' && line.outcome.kind === 'accepted')
    expect(accepted.map((line) => line.outcome.gesture)).toEqual(['Open_Palm', 'Thumb_Up'])
    expect(lines.filter((line) => line.kind === 'recognizer').length).toBe(rig.source.recognized.length)
    expect(lines.slice(1).every((line) => typeof line.t === 'number' && typeof line.wall_t === 'number')).toBe(true)
    expect(get().producer.view.recording.size).toBe(lines.length - 1)

    act(() => get().producer.clearRecording())
    expect(get().producer.view.recording.size).toBe(0)
  })

  test('every payload the webcam source sends passes the console Intent v1 mirror', async () => {
    const { clients, hold, enable } = await mount()
    await enable()
    hold('Open_Palm', 650)
    hold(null, 250)
    hold('Thumb_Up', 450)
    hold(null, 250)
    hold('Closed_Fist', 650)
    hold(null, 250)
    hold('Thumb_Up', 450)

    const sent = clients.webcam?.sent ?? []
    expect(sent.map((intent) => intent.name)).toEqual(['capture_room', 'hold'])
    sent.forEach((intent) => {
      expect(intent.source).toBe('webcam')
      expect(isConsoleIntentV1(intent)).toBe(true)
    })
  })
})

describe('emissionBlockedReason', () => {
  const link = (status: ConnectionStatus) => ({ status, transport: 'fixture' as const, changedAt: 0 })

  test('answers for the console connection before the webcam source', () => {
    const base = createInitialControlState(session, 0)
    const blocked = (console: ConnectionStatus, webcam: ConnectionStatus) =>
      emissionBlockedReason(
        { state: { ...base, connection: link(console), webcamConnection: link(webcam) }, pendingRequest: null },
        null,
        'kitchen-01',
      )
    expect(blocked('disconnected', 'disconnected')).toBe(
      'The console connection is disconnected; no gesture intent can be drafted.',
    )
    expect(blocked('degraded', 'connected')).toBe(
      'The console connection is degraded; no gesture intent can be drafted.',
    )
    expect(blocked('connected', 'disconnected')).toBe(
      'The webcam relay source is not connected; no gesture intent can be sent.',
    )
    // An empty roster has no class to name, so the reason says device.
    expect(blocked('connected', 'connected')).toBe('Select at least one ready device.')
  })
})


describe('mixed fleet gestures', () => {
  test.each([
    ['Pointing_Up', { dx: 0, dy: 1 }],
    ['Victory', { dx: 1, dy: 0 }],
    ['Closed_Fist', { dx: 0, dy: -1 }],
    ['ILoveYou', { dx: -1, dy: 0 }],
  ] as const)('%s translates an aircraft subset of a mixed roster only after confirmation', async (pose, args) => {
    const { clients, get, hold, enable } = await mount({ profile: 'fleet', mixed: true })
    await act(async () => { get().control.issueIntent({ name: 'select', args: { ids: [1] }, targets: [1] }) })
    await enable()
    hold(pose, 650)
    const preview = get().control.pendingRequest?.intent
    expect(preview).toMatchObject({ name: 'translate', args, selection: [1], source: 'webcam', confirm: false })
    expect(clients.webcam?.sent).toHaveLength(0)
    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({ ...preview, confirm: true, t: expect.any(Number) })
    expect(clients.console.sent.map((intent) => intent.name)).toEqual(['select'])
    expect(clients.keyboard.sent).toHaveLength(0)
  })

  test('changing selection invalidates a pending movement before thumb confirmation', async () => {
    const { clients, get, hold, enable } = await mount({ profile: 'fleet', mixed: true })
    await enable()
    hold('Victory', 650)
    expect(get().control.pendingRequest?.intent.name).toBe('translate')
    await act(async () => { get().control.issueIntent({ name: 'select', args: { ids: [11] }, targets: [11] }) })
    expect(get().control.pendingRequest).toBeNull()
    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(0)
  })

  test.each([false, true])('swarm formation refuses a mixed selection, C2=%s', async (c2) => {
    const { clients, get, hold, enable } = await mount({ profile: 'swarm', mixed: true, c2 })
    await act(async () => { get().control.issueIntent({ name: 'select', args: { ids: [1, 11, 12] }, targets: [1, 11, 12] }) })
    await enable()
    hold('Victory', 650)
    if (!c2) {
      expect(get().control.pendingRequest).toBeNull()
      expect(get().producer.view.lastAction?.detail).toContain('disabled by relay capability profile')
      return
    }
    expect(get().control.pendingRequest).toBeNull()
    expect(get().producer.view.lastAction?.detail).toContain('Select only aircraft')
    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(0)
  })
})


describe('ground gesture confirmation through the real controller', () => {
  test('dwell drafts a bounded ground pulse, neutral then thumb up sends only on webcam', async () => {
    const { rig, clients, get, hold, enable } = await mount({ profile: 'ground' })
    const t = rig.dependencies.clock.wall()
    act(() => { clients.console.emitServer(fieldGroundState(t, session)); clients.console.emitServer(fieldGroundPose(t, session)); clients.console.emitServer(fieldGroundState(t + 1, session)) })
    await enable()
    hold('Pointing_Up', 650)
    const draft = get().control.pendingRequest?.intent
    expect(draft).toMatchObject({ name: 'ground_velocity', selection: [11], source: 'webcam', confirm: false,
      args: { linear_mm_s: 80, angular_mrad_s: 0, duration_ms: 250 } })
    expect(clients.webcam?.sent).toEqual([])
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toEqual([])
    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({ ...draft, t: expect.any(Number), confirm: true })
    expect(clients.console.sent).toEqual([])
    expect(clients.keyboard.sent).toEqual([])
  })

  test('loss of drive authority after dwell prevents gesture confirmation', async () => {
    const { rig, clients, get, hold, enable } = await mount({ profile: 'ground' })
    const t = rig.dependencies.clock.wall()
    act(() => { clients.console.emitServer(fieldGroundState(t, session)); clients.console.emitServer(fieldGroundPose(t, session)); clients.console.emitServer(fieldGroundState(t + 1, session)) })
    await enable(); hold('Victory', 650)
    expect(get().control.pendingRequest?.intent.name).toBe('ground_velocity')
    const next = fieldGroundState(rig.dependencies.clock.wall(), session)
    next.drones[0].control_authority = false
    act(() => { clients.console.emitServer(next) })
    hold(null, 250); hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toEqual([])
    expect(clients.console.sent).toEqual([])
  })
})


test('fleet translation gestures refuse a mixed selection without sending a preview', async () => {
  const { clients, get, hold, enable } = await mount({ profile: 'fleet', mixed: true })
  await act(async () => { get().control.issueIntent({ name: 'select', args: { ids: [1, 11] }, targets: [1, 11] }) })
  await enable(); hold('Pointing_Up', 650)
  expect(get().control.pendingRequest).toBeNull()
  expect(get().producer.view.lastAction?.detail).toContain('Select only aircraft')
  hold(null, 250); hold('Thumb_Up', 450)
  expect(clients.webcam?.sent).toEqual([])
})
