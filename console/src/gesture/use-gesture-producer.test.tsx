import { act, render } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, test } from 'vitest'
import { createInitialControlState, type ConnectionStatus } from '../control/state'
import { useControlConsole, type ControlClients } from '../control/use-control-console'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1, type RelayStateEvent } from '../relay/contract'
import { FixtureRelayClient } from '../testing/fixture-relay-client'
import { createGestureTestRig, type GestureTestRig } from '../testing/gesture-fixtures'
import { DEFAULT_GESTURE_PAIRS, type GestureCategory } from './policy'
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
}: {
  clients: ControlClients
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
  const producer = useGestureProducer({ control, roomId, dependencies: rig.dependencies })
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

function fixtureClients(rig: GestureTestRig, withWebcam = true): FixtureClients {
  const wall = () => rig.dependencies.clock.wall()
  return {
    console: new FixtureRelayClient(session, wall, 'console'),
    keyboard: new FixtureRelayClient(session, wall, 'keyboard'),
    ...(withWebcam ? { webcam: new FixtureRelayClient(session, wall, 'webcam') } : {}),
  }
}

async function mount(options: { loadError?: Error; withWebcam?: boolean; roomId?: string } = {}) {
  const rig = createGestureTestRig({ loadError: options.loadError })
  const clients = fixtureClients(rig, options.withWebcam ?? true)
  const latest: { current: Latest | null } = { current: null }
  render(
    <Harness
      clients={clients}
      rig={rig}
      roomId={options.roomId ?? 'room-01'}
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
  let stateSequence = 0
  const publishState = (changes: Partial<RelayStateEvent>) => {
    const { state } = get().control
    const { formation, spacing, capabilityProfile } = state
    if (formation === null || spacing === null || capabilityProfile === null) {
      throw new Error('The fixture must publish its initial authoritative state first.')
    }
    act(() => clients.console.emitServer({
      v: 1,
      t: rig.dependencies.clock.wall(),
      event_id: `gesture-authoritative-state-${++stateSequence}`,
      type: 'state',
      session,
      roster_version: state.rosterVersion,
      armed: state.armed,
      estop: state.estop,
      selection: state.selection,
      formation,
      spacing,
      mode: 'indoor',
      capability_profile: capabilityProfile,
      enabled_intent_names: state.enabledIntentNames,
      pending: null,
      accepted_plan: null,
      drones: Object.values(state.aircraft),
      ...changes,
    }))
  }
  return { rig, clients, get, hold, enable, publishState }
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

  test('two selected aircraft can HOLD while capture is unavailable, and a held thumb confirms the exact draft once', async () => {
    const { rig, clients, get, hold, enable, publishState } = await mount()
    publishState({ selection: [1, 2] })
    await enable()
    const readiness = () => get().producer.view.actionReadiness
    expect(readiness().find(({ pair }) => pair.gesture === 'Open_Palm')).toMatchObject({
      blockedReason: 'Select exactly one ready aircraft for capture_room.',
    })
    expect(readiness().find(({ pair }) => pair.gesture === 'Closed_Fist')).toMatchObject({
      blockedReason: null,
      targets: [1, 2],
    })

    hold('Closed_Fist', 650)
    const draft = get().control.pendingRequest?.intent
    expect(draft).toMatchObject({ name: 'hold', source: 'webcam', confirm: false, selection: [1, 2], args: {} })
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(readiness().find(({ pair }) => pair.gesture === 'Thumb_Up')).toMatchObject({
      blockedReason: null,
      targets: [1, 2],
    })
    expect(readiness().find(({ pair }) => pair.gesture === 'Closed_Fist')?.blockedReason).toContain('already pending')

    hold(null, 250)
    hold('Thumb_Up', 450)
    const confirmedAt = rig.dependencies.clock.wall()
    hold('Thumb_Up', 1_000)
    expect(clients.webcam?.sent).toEqual([{ ...draft, t: confirmedAt, confirm: true }])
    expect(clients.console.sent).toHaveLength(0)
    expect(get().control.pendingRequest).toBeNull()
    expect(get().producer.view.outcome.kind).toBe('duplicate_suppressed')
  })

  test.each(['selection', 'roster'] as const)('a %s change invalidates the fleet HOLD preview without retargeting or sending it', async (change) => {
    const { clients, get, hold, enable, publishState } = await mount()
    publishState({ selection: [1, 2] })
    await enable()
    hold('Closed_Fist', 650)
    const draft = get().control.pendingRequest!.intent
    hold(null, 250)
    publishState(change === 'selection'
      ? { selection: [2] }
      : { roster_version: get().control.state.rosterVersion + 1 })

    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
    expect(get().control.pendingRequest).toBeNull()
    expect(get().control.state.requests.find(({ intent }) => intent.intent_id === draft.intent_id)).toMatchObject({
      intent: { selection: [1, 2] },
      status: 'invalidated',
      reasonCode: change === 'selection' ? 'selection_changed' : 'stale_roster',
    })
  })

  test('each draft reports its relay capability and room requirements without blocking other gestures', async () => {
    const { get, hold, clients, enable, publishState } = await mount({ roomId: 'INVALID ROOM' })
    await enable()
    const readiness = () => get().producer.view.actionReadiness
    expect(readiness().find(({ pair }) => pair.gesture === 'Open_Palm')?.blockedReason).toContain('valid room identifier')
    expect(readiness().find(({ pair }) => pair.gesture === 'Closed_Fist')?.blockedReason).toBeNull()
    publishState({ enabled_intent_names: get().control.state.enabledIntentNames.filter((name) => name !== 'hold') })
    expect(readiness().find(({ pair }) => pair.gesture === 'Closed_Fist')?.blockedReason).toContain('hold is disabled')
    hold('Closed_Fist', 650)
    expect(get().control.pendingRequest).toBeNull()
    expect(clients.webcam?.sent).toHaveLength(0)
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
    hold('Open_Palm', 1000, 0.6)
    expect(get().control.pendingRequest).toBeNull()
    expect(get().control.state.requests).toHaveLength(0)
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(get().producer.view.notable?.outcome).toMatchObject({ kind: 'low_confidence', score: 0.6 })
    expect(get().producer.view.phase).toBe('idle')
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
    get().producer.view.actionReadiness.forEach(({ blockedReason }) => expect(blockedReason).toContain('unplugged'))
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
        { state: { ...base, capabilityProfile: 'c1_basic_control', enabledIntentNames: [...C1_BASIC_CONTROL_INTENTS], connection: link(console), webcamConnection: link(webcam) }, pendingRequest: null },
        DEFAULT_GESTURE_PAIRS[0],
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
    expect(blocked('connected', 'connected')).toBe('Select at least one ready aircraft.')
  })
})
