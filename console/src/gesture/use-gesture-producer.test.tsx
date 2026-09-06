import { act, render } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { describe, expect, test } from 'vitest'
import { createInitialControlState, type ConnectionStatus } from '../control/state'
import { useControlConsole, type ControlClients } from '../control/use-control-console'
import { C1_BASIC_CONTROL_INTENTS, isConsoleIntentV1 } from '../relay/contract'
import { FixtureRelayClient, fixtureAircraft } from '../testing/fixture-relay-client'
import { createGestureTestRig, type GestureTestRig } from '../testing/gesture-fixtures'
import type { GestureCategory } from './policy'
import { emissionBlockedReason, useGestureProducer } from './use-gesture-producer'

/** The three flight pairs, each drafted into the dock with source webcam. */
const FLIGHT_PAIRS: ReadonlyArray<readonly [GestureCategory, 'takeoff' | 'translate' | 'land', object]> = [
  ['Pointing_Up', 'takeoff', {}],
  ['Victory', 'translate', { dx: 1, dy: 0 }],
  ['ILoveYou', 'land', {}],
]

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
  const sequence = useRef(0)
  const control = useControlConsole({
    sessionId: session,
    clients,
    intentDependencies: {
      now: () => rig.dependencies.clock.wall(),
      nextId: () => `gesture-intent-${++sequence.current}`,
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
    expect(lines[0].pairs).toHaveLength(7)
    expect(lines[0].pairs.map((pair: { gesture: string; action: string }) => [pair.gesture, pair.action])).toEqual([
      ['Open_Palm', 'draft:capture_room'],
      ['Closed_Fist', 'draft:hold'],
      ['Pointing_Up', 'draft:takeoff'],
      ['Victory', 'draft:translate'],
      ['ILoveYou', 'draft:land'],
      ['Thumb_Up', 'confirm'],
      ['Thumb_Down', 'cancel'],
    ])
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
    for (const [gesture] of FLIGHT_PAIRS) {
      hold(null, 250)
      hold(gesture, 650)
      hold(null, 250)
      hold('Thumb_Up', 450)
    }

    const sent = clients.webcam?.sent ?? []
    expect(sent.map((intent) => intent.name)).toEqual(['capture_room', 'hold', 'takeoff', 'translate', 'land'])
    expect(sent.map((intent) => intent.args)).toEqual([
      expect.objectContaining({ pattern: 'pano_360' }),
      {},
      {},
      { dx: 1, dy: 0 },
      {},
    ])
    sent.forEach((intent) => {
      expect(intent.source).toBe('webcam')
      expect(intent.confirm).toBe(true)
      expect(isConsoleIntentV1(intent)).toBe(true)
    })
    expect(new Set(sent.map((intent) => intent.intent_id)).size).toBe(5)
  })
})

describe('useGestureProducer flight pairs', () => {
  /** A console state frame over the fixture roster with the control fields overridden. */
  const emitState = (
    clients: FixtureClients,
    wall: () => number,
    overrides: { estop?: boolean; selection?: number[] },
  ) => {
    act(() =>
      clients.console.emitServer({
        v: 1,
        t: wall(),
        event_id: `producer-state-${wall()}`,
        type: 'state',
        session,
        roster_version: 7,
        armed: true,
        estop: overrides.estop ?? false,
        selection: overrides.selection ?? [1],
        formation: 'none',
        spacing: 0.8,
        mode: 'indoor',
        capability_profile: 'c1_basic_control',
        enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
        pending: null,
        accepted_plan: null,
        drones: fixtureAircraft(wall(), 4),
      }),
    )
  }

  test('pointing up drafts a webcam takeoff into the dock and a thumb up confirms the same intent_id', async () => {
    const { rig, clients, get, hold, enable } = await mount()
    await enable()

    hold('Pointing_Up', 650)
    const pending = get().control.pendingRequest
    expect(pending).not.toBeNull()
    expect(pending?.intent).toMatchObject({
      name: 'takeoff',
      source: 'webcam',
      confirm: false,
      args: {},
      selection: [1],
    })
    expect(pending?.status).toBe('pending_confirmation')
    expect(pending?.plan?.title).toBe('Takeoff')
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'draft',
      intentId: pending?.intent.intent_id,
      detail: 'Pointing_Up drafted takeoff for preview; nothing sent.',
    })

    hold(null, 250)
    hold('Thumb_Up', 450)
    const sent = clients.webcam?.sent ?? []
    expect(sent).toHaveLength(1)
    expect(sent[0]).toMatchObject({
      intent_id: pending?.intent.intent_id,
      name: 'takeoff',
      source: 'webcam',
      confirm: true,
      selection: [1],
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

  test('a victory sign drafts one forward translate step into the dock and sends nothing until confirmed', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()

    hold('Victory', 650)
    const pending = get().control.pendingRequest
    expect(pending?.intent).toMatchObject({
      name: 'translate',
      source: 'webcam',
      confirm: false,
      args: { dx: 1, dy: 0 },
      selection: [1],
    })
    expect(pending?.status).toBe('pending_confirmation')
    expect(pending?.plan?.title).toBe('Translate')
    expect(pending?.plan?.steps[0]).toBe("Move D-01 by dx 1, dy 0 steps in the planner's translation frame.")
    expect(get().control.state.requests).toHaveLength(1)
    expect(get().control.state.requests[0].status).toBe('pending_confirmation')
    expect(get().control.state.requests[0].timestamps.sent).toBeUndefined()
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'draft',
      detail: 'Victory drafted translate forward one step for preview; nothing sent.',
    })

    hold('Victory', 600)
    expect(get().control.state.requests).toHaveLength(1)
    expect(clients.webcam?.sent).toHaveLength(0)

    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({
      intent_id: pending?.intent.intent_id,
      name: 'translate',
      source: 'webcam',
      args: { dx: 1, dy: 0 },
      confirm: true,
    })
    expect(isConsoleIntentV1(clients.webcam?.sent[0])).toBe(true)
    expect(clients.console.sent).toHaveLength(0)
  })

  test('the I-love-you sign drafts land into the dock and a thumb up confirms it', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()

    hold('ILoveYou', 650)
    const pending = get().control.pendingRequest
    expect(pending?.intent).toMatchObject({ name: 'land', source: 'webcam', confirm: false, args: {}, selection: [1] })
    expect(pending?.plan?.title).toBe('Land')
    expect(clients.webcam?.sent).toHaveLength(0)

    hold(null, 250)
    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({
      intent_id: pending?.intent.intent_id,
      name: 'land',
      source: 'webcam',
      confirm: true,
    })
    expect(isConsoleIntentV1(clients.webcam?.sent[0])).toBe(true)
    expect(get().control.state.requests[0]).toMatchObject({ status: 'accepted', intent: { name: 'land' } })
  })

  test('a thumb down cancels each flight preview and nothing is sent', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()

    for (const [gesture, name] of FLIGHT_PAIRS) {
      hold(gesture, 650)
      const pending = get().control.pendingRequest
      expect(pending?.intent).toMatchObject({ name, source: 'webcam', confirm: false })
      hold(null, 250)
      hold('Thumb_Down', 450)
      expect(get().control.pendingRequest).toBeNull()
      expect(get().control.state.requests[0]).toMatchObject({
        status: 'cancelled',
        intent: { intent_id: pending?.intent.intent_id, name },
      })
      expect(get().producer.view.lastAction).toMatchObject({
        kind: 'cancel',
        intentId: pending?.intent.intent_id,
        detail: `Thumb_Down cancelled the ${name} preview; nothing sent.`,
      })
      hold(null, 250)
    }
    expect(get().control.state.requests).toHaveLength(3)
    expect(get().control.state.requests.every((request) => request.status === 'cancelled')).toBe(true)
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)
  })

  test('flight gestures draft nothing without a non-empty ready selection', async () => {
    const { rig, clients, get, hold, enable } = await mount()
    await enable()
    const wall = () => rig.dependencies.clock.wall()

    emitState(clients, wall, { selection: [] })
    expect(get().control.state.selection).toEqual([])
    expect(get().producer.view.emissionBlockedReason).toBe('Select at least one ready aircraft.')
    for (const [gesture, name] of FLIGHT_PAIRS) {
      hold(gesture, 650)
      expect(get().control.pendingRequest).toBeNull()
      expect(get().producer.view.lastAction).toMatchObject({
        kind: 'blocked',
        intentId: null,
        detail: 'Select at least one ready aircraft.',
      })
      expect(get().control.state.requests).toHaveLength(0)
      hold(null, 250)
      void name
    }

    // The reducer keeps only selectable aircraft from a state frame, so a
    // degraded D-03 in the relay's selection leaves nothing to draft against.
    emitState(clients, wall, { selection: [3] })
    expect(get().control.state.aircraft[3]?.selectable).toBe(false)
    expect(get().control.state.selection).toEqual([])
    expect(get().producer.view.emissionBlockedReason).toBe('Select at least one ready aircraft.')
    hold('Pointing_Up', 650)
    expect(get().control.state.requests).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({
      kind: 'blocked',
      detail: 'Select at least one ready aircraft.',
    })
    expect(clients.webcam?.sent).toHaveLength(0)
    expect(clients.console.sent).toHaveLength(0)

    // The pure check still names the aircraft when a selection holds one that is not ready.
    const state = get().control.state
    expect(
      emissionBlockedReason({ state: { ...state, selection: [3] }, pendingRequest: null }, null, 'room-01'),
    ).toBe('D-03 is not ready or selectable.')
  })

  test('an active network stop blocks flight drafting with the reason stated', async () => {
    const { rig, clients, get, hold, enable } = await mount()
    await enable()

    emitState(clients, () => rig.dependencies.clock.wall(), { estop: true })
    expect(get().producer.view.emissionBlockedReason).toBe(
      'The network stop is active; no gesture intent can be drafted until the relay reports it clear.',
    )
    hold('Victory', 650)
    expect(get().control.state.requests).toHaveLength(0)
    expect(get().producer.view.lastAction).toMatchObject({ kind: 'blocked' })
    expect(clients.webcam?.sent).toHaveLength(0)
  })

  test('the control flow refuses a webcam flight draft that the hook would not park in the dock', async () => {
    const { clients, get, hold, enable } = await mount()
    await enable()

    let refused: unknown = 'unset'
    act(() => {
      refused = get().control.issueIntent({ name: 'translate', args: { dx: 1, dy: 0 }, source: 'webcam', targets: [] })
    })
    expect(refused).toBeNull()
    expect(get().control.state.requests).toHaveLength(0)

    let parked: unknown = null
    act(() => {
      parked = get().control.issueIntent({ name: 'translate', args: { dx: 1, dy: 0 }, source: 'webcam' })
    })
    expect(parked).toMatchObject({ name: 'translate', source: 'webcam', confirm: false })
    expect(get().control.pendingRequest?.status).toBe('pending_confirmation')
    expect(clients.webcam?.sent).toHaveLength(0)

    hold('Thumb_Up', 450)
    expect(clients.webcam?.sent).toHaveLength(1)
    expect(clients.webcam?.sent[0]).toMatchObject({ name: 'translate', source: 'webcam', confirm: true })
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
    expect(blocked('connected', 'connected')).toBe('Select at least one ready aircraft.')
  })
})
