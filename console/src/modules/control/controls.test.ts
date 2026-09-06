import { describe, expect, test } from 'vitest'
import { controlReducer, createInitialControlState, createRequestRecord, type ControlState } from '../../control/state'
import { createIntent } from '../../control/intent'
import {
  C1_BASIC_CONTROL_INTENTS,
  C2_FLEET_OPERATIONS_INTENTS,
  type RelayStateEvent,
} from '../../relay/contract'
import { fixtureAircraft } from '../../testing/fixture-relay-client'
import {
  DPAD_CELLS,
  MISSION_STEPS,
  NO_SELECTION_REASON,
  STOP_ACTIVE_REASON,
  aircraftChips,
  altitudeControls,
  captureFlow,
  captureGate,
  chipBlockers,
  commandCatalog,
  compassSectors,
  dpadBlockedReason,
  fanoutFor,
  fleetControls,
  formationControls,
  formationPlot,
  formationRelayNote,
  formationSlots,
  gateRows,
  guidanceNote,
  motionControls,
  requestTone,
  retryBlockedReason,
  sectorSummary,
  type CaptureReadiness,
} from './controls'

const session = 'controls-test'
const t = 1_756_700_000_000

function stateEvent(selection: number[], overrides: Partial<RelayStateEvent> = {}): RelayStateEvent {
  return {
    v: 1,
    t,
    type: 'state',
    event_id: `state-${selection.join('-')}-${overrides.event_id ?? 'x'}`,
    session,
    roster_version: 7,
    armed: true,
    estop: false,
    selection,
    formation: 'none',
    spacing: 0.8,
    mode: 'indoor',
    capability_profile: 'c1_basic_control',
    enabled_intent_names: [...C1_BASIC_CONTROL_INTENTS],
    pending: null,
    accepted_plan: null,
    drones: fixtureAircraft(t),
    ...overrides,
  }
}

function connected(selection: number[], overrides: Partial<RelayStateEvent> = {}): ControlState {
  let state = createInitialControlState(session, t)
  state = controlReducer(state, {
    type: 'connection_changed',
    connection: { status: 'connected', transport: 'fixture', changedAt: t },
  })
  return controlReducer(state, { type: 'relay_event', event: stateEvent(selection, overrides) })
}

const guidance: CaptureReadiness = {
  guidance_mode: 'visual_advisory',
  pose_source: 'visual_odometry',
  pose_ok: true,
  clearance_ok: true,
  camera_ok: true,
  storage_ok: true,
  motion_ok: true,
  image_quality_ok: true,
  coverage: ['accepted', 'accepted', 'weak', 'unseen', 'unseen', 'accepted', 'weak', 'accepted'],
  next_heading_deg: 135,
  suggested_delta: 'yaw +42°',
}

describe('control gating', () => {
  test('the advertised profile disables non-safety controls the relay did not enable', () => {
    const state = connected([1], {
      capability_profile: 'land-only',
      enabled_intent_names: ['land'],
    })
    const byKey = Object.fromEntries(
      [...fleetControls(state), ...motionControls(state)].map((spec) => [spec.key, spec]),
    )

    expect(byKey.arm).toMatchObject({
      enabled: false,
      note: 'arm is disabled by relay capability profile land-only.',
    })
    expect(byKey.hold).toMatchObject({ enabled: false })
    expect(dpadBlockedReason(state)).toBe(
      'translate is disabled by relay capability profile land-only.',
    )
    expect(captureGate(state, 'room-1', true, guidance)).toMatchObject({
      ready: false,
      text: 'capture_room is disabled by relay capability profile land-only.',
    })
  })

  test('disconnected: every control is disabled with the connection reason', () => {
    const state = createInitialControlState(session, t)
    for (const spec of [...fleetControls(state), ...motionControls(state)]) {
      expect(spec.enabled).toBe(false)
      expect(spec.note).toBe('The console connection is disconnected. Nothing can be sent.')
    }
    expect(dpadBlockedReason(state)).toBe('The console connection is disconnected. Nothing can be sent.')
  })

  test('connected with one ready aircraft selected: advertised controls send or confirm', () => {
    const state = connected([1])
    const byKey = Object.fromEntries([...fleetControls(state), ...motionControls(state)].map((s) => [s.key, s]))
    expect(byKey.arm).toMatchObject({ enabled: true, badge: '', note: 'Sends immediately on the console connection.' })
    expect(byKey.disarm).toMatchObject({ enabled: false, badge: '', note: 'disarm is disabled by relay capability profile c1_basic_control.', noteTone: 'warn' })
    expect(byKey['select-all']).toMatchObject({ enabled: true, note: 'Selects every ready aircraft.', press: { name: 'select', args: { ids: [1, 2, 4] }, targets: [1, 2, 4] } })
    expect(byKey.takeoff).toMatchObject({ enabled: true, confirm: true, badge: 'confirm', note: 'Confirmation required before send.' })
    expect(byKey.hold).toMatchObject({ enabled: true, badge: '', rule: 'selected' })
    expect(byKey.land_all).toMatchObject({ enabled: true, confirm: true, press: { targets: [1, 2, 3, 4] }, note: 'Confirmation required. Targets every aircraft in the roster.' })
    expect(byKey.sweep).toMatchObject({ enabled: false, note: 'sweep is disabled by relay capability profile c1_basic_control.' })
    expect(byKey['spacing-']).toMatchObject({ enabled: false, press: { name: 'spacing', args: { delta: -1 } } })
    expect(byKey['spacing+']).toMatchObject({ enabled: false, press: { name: 'spacing', args: { delta: 1 } } })
    expect(byKey.formation_next).toMatchObject({ enabled: false, press: { name: 'formation_next', args: {} } })
    expect(dpadBlockedReason(state)).toBeNull()
  })

  test('no selection: capability refusal precedes selection checks', () => {
    const state = connected([])
    const byKey = Object.fromEntries([...fleetControls(state), ...motionControls(state)].map((s) => [s.key, s]))
    expect(byKey.takeoff).toMatchObject({ enabled: false, note: NO_SELECTION_REASON })
    expect(byKey.sweep).toMatchObject({ enabled: false, note: 'sweep is disabled by relay capability profile c1_basic_control.' })
    expect(byKey.arm.enabled).toBe(true)
    expect(byKey.disarm).toMatchObject({ enabled: false })
    expect(byKey.land_all.enabled).toBe(true)
    expect(dpadBlockedReason(state)).toBe(NO_SELECTION_REASON)
  })

  test('a selected aircraft that is not ready blocks the ready-gated controls by name', () => {
    const state: ControlState = { ...connected([1]), selection: [1, 3] }
    const byKey = Object.fromEntries(motionControls(state).map((s) => [s.key, s]))
    expect(byKey.takeoff.note).toBe('D-03 is not ready.')
    expect(byKey.hold.enabled).toBe(false)
    expect(byKey.sweep).toMatchObject({ enabled: false })
  })

  test('stop active: motion is blocked with the stop reason, the pad follows stop before selection', () => {
    const state = connected([], { estop: true })
    const byKey = Object.fromEntries([...fleetControls(state), ...motionControls(state)].map((s) => [s.key, s]))
    expect(byKey.arm).toMatchObject({ enabled: false, note: STOP_ACTIVE_REASON })
    expect(byKey.land_all).toMatchObject({ enabled: true, note: 'Confirmation required. Targets every aircraft in the roster.' })
    expect(byKey.disarm).toMatchObject({ enabled: false })
    expect(dpadBlockedReason(state)).toBe(STOP_ACTIVE_REASON)
  })

  test('select all ready is blocked when nothing is ready', () => {
    const drones = fixtureAircraft(t).map((drone) => ({ ...drone, membership: 'degraded' as const, selectable: false }))
    const state = connected([], { drones })
    expect(fleetControls(state)[2]).toMatchObject({ enabled: false, note: 'No aircraft is ready.' })
  })

  test('C1 permits altitude while holding formation controls for C2', () => {
    const withSelection = connected([1])
    expect(formationControls(withSelection).map((s) => s.label)).toEqual([
      'line',
      'column',
      'wedge',
      'diamond',
    ])
    expect(formationControls(withSelection)[2]).toMatchObject({ enabled: false, note: 'formation_set is disabled by relay capability profile c1_basic_control.', press: { name: 'formation_set', args: { name: 'wedge' } } })
    expect(altitudeControls(withSelection)[0]).toMatchObject({ enabled: true, press: { name: 'altitude', args: { delta: 1 } } })
    expect(altitudeControls(connected([]))[1]).toMatchObject({ enabled: false, note: NO_SELECTION_REASON })
  })

  test('C2 formation controls enforce the exact two-to-six and four-to-six bounds', () => {
    const c2 = {
      capability_profile: 'c2_fleet_operations',
      enabled_intent_names: [...C2_FLEET_OPERATIONS_INTENTS],
    }
    const one = connected([1], c2)
    expect(motionControls(one).find((control) => control.key === 'formation_next')).toMatchObject({
      enabled: false,
      note: 'formation requires at least 2 selected aircraft.',
    })
    expect(formationControls(one)[0]).toMatchObject({
      enabled: false,
      note: 'line formation requires at least 2 selected aircraft.',
    })
    expect(formationControls(one)[2]).toMatchObject({
      enabled: false,
      note: 'wedge formation requires at least 4 selected aircraft.',
    })
    const two = connected([1, 2], c2)
    expect(formationControls(two).map((control) => control.enabled)).toEqual([
      true,
      true,
      false,
      false,
    ])
    const fourReady = fixtureAircraft(t).map((drone) => ({
      ...drone,
      membership: 'ready' as const,
      readiness_reasons: [],
      selectable: true,
    }))
    expect(
      formationControls(connected([1, 2, 3, 4], { ...c2, drones: fourReady })).every(
        (control) => control.enabled,
      ),
    ).toBe(true)
  })

  test('C2 labels disarm as a grounded session-authorization change', () => {
    const state = connected([1], {
      capability_profile: 'c2_fleet_operations',
      enabled_intent_names: [...C2_FLEET_OPERATIONS_INTENTS],
    })

    expect(fleetControls(state)[1]).toMatchObject({
      enabled: true,
      badge: '',
      note:
        'Withdraws session arm authorization only after the relay proves the fleet grounded; it does not command the aircraft.',
    })
  })
})

describe('command catalogue', () => {
  test('lists Fleet and Motion rows in the design order with confirmation, rule and status', () => {
    const groups = commandCatalog(connected([1]))
    expect(groups.map((group) => group.title)).toEqual(['Fleet', 'Motion'])
    expect(groups[0].rows.map((row) => row.label)).toEqual(['Arm', 'Disarm', 'Select all'])
    expect(groups[1].rows.map((row) => [row.label, row.confirm, row.rule, row.status])).toEqual([
      ['Takeoff', 'confirm', 'selected', 'available'],
      ['Hold', '—', 'selected', 'available'],
      ['Come home', '—', 'selected', 'available'],
      ['Land', 'confirm', 'selected', 'available'],
      ['Land all', 'confirm', 'all', 'available'],
      ['Formation next', '—', 'selected', 'available'],
      ['Spacing tighter', '—', 'selected', 'available'],
      ['Spacing wider', '—', 'selected', 'available'],
      ['Sweep', 'confirm', 'selected', 'available'],
      ['Survey area', 'confirm', 'any', 'later'],
      ['Map area', 'confirm', 'non-empty', 'later'],
    ])
    const later = groups[1].rows.filter((row) => row.status === 'later')
    expect(later.every((row) => !row.enabled && row.spec === null)).toBe(true)
    const land = groups[1].rows.find((row) => row.label === 'Land')
    expect(land).toMatchObject({ enabled: true, note: 'Confirmation required before send.' })
  })
})

describe('translate pad and formation geometry', () => {
  test('nine cells with the four directions and aria-hidden spacers', () => {
    expect(DPAD_CELLS).toHaveLength(9)
    expect(DPAD_CELLS.filter((cell) => cell.direction).map((cell) => cell.aria)).toEqual([
      'Translate north',
      'Translate west',
      'Translate east',
      'Translate south',
    ])
    expect(DPAD_CELLS[4]).toMatchObject({ label: '·', direction: null })
  })

  test('slots follow the design formulas', () => {
    for (const name of ['line', 'column', 'wedge', 'diamond'] as const) {
      for (const count of [4, 5, 6]) {
        const slots = formationSlots(name, count, 1.5)
        expect(slots).toHaveLength(count)
        expect(slots.reduce((total, [x]) => total + x, 0)).toBeCloseTo(0)
        expect(slots.reduce((total, [, y]) => total + y, 0)).toBeCloseTo(0)
        const distances = slots.flatMap((first, firstIndex) =>
          slots
            .slice(firstIndex + 1)
            .map((second) => Math.hypot(first[0] - second[0], first[1] - second[1])),
        )
        expect(Math.min(...distances)).toBeCloseTo(1.5 * 1.01)
      }
    }
    expect(formationSlots('line', 2, 1.5)[0][0]).toBeCloseTo(-0.7575)
    expect(formationSlots('column', 3, 1)).toEqual([
      [0, -1.01],
      [0, 0],
      [0, 1.01],
    ])
    const wedge = formationSlots('wedge', 4, 1)
    expect(wedge[0][0]).toBeCloseTo(-0.505)
    expect(wedge[0][1]).toBeCloseTo(0.505)
    const diamond = formationSlots('diamond', 4, 1)
    expect(diamond[0][0]).toBeCloseTo(0)
    expect(diamond[0][1]).toBeCloseTo(1.01 / Math.sqrt(2))
    for (const invalid of ['circle', 'grid', 'V', 'unknown']) {
      expect(formationSlots(invalid, 4, 1)).toEqual([])
    }
    expect(formationSlots('line', 1, 1)).toEqual([])
    expect(formationSlots('wedge', 3, 1)).toEqual([])
    expect(formationSlots('diamond', 7, 1)).toEqual([])
    expect(formationSlots('line', 2, Number.NaN)).toEqual([])
  })

  test('the plot places the selected aircraft and labels unreported spacing honestly', () => {
    const aircraft = fixtureAircraft(t).filter((drone) => [1, 2].includes(drone.drone_id))
    const dots = formationPlot(aircraft, 'line', 1.5)
    expect(dots.map((dot) => dot.id)).toEqual(['D-01', 'D-02'])
    expect(Number.parseFloat(dots[0].left)).toBeCloseTo(
      50 + (-0.7575 / (1.2 * 2.4)) * 100,
    )
    expect(dots[0].slot).toBe('slot 1 · -0.8 m, 0.0 m')
    expect(formationPlot(aircraft, 'line', null)[1].slot).toBe('slot 2 · spacing unreported')
    expect(formationPlot(aircraft, null, 1.5)).toEqual([])
  })

  test('the relay note distinguishes preview from report', () => {
    expect(formationRelayNote(null, 'line')).toBe('The relay reports line.')
    expect(formationRelayNote('line', 'line')).toBe('The relay reports line.')
    expect(formationRelayNote('diamond', 'line')).toBe(
      'Requested diamond. The relay still reports line until execution completes.',
    )
    expect(formationRelayNote(null, null)).toBe('The relay has not reported a formation.')
  })

  test('fan-out names one command per target aircraft', () => {
    expect(fanoutFor('land_all', {}, [1, 2])).toEqual([
      { id: 'D-01', cmd: 'land in place, motors off at touchdown' },
      { id: 'D-02', cmd: 'land in place, motors off at touchdown' },
    ])
    expect(fanoutFor('capture_room', { room_id: 'r', capture_id: 'c', pattern: 'pano_360' }, [1])).toEqual([
      { id: 'D-01', cmd: 'hold, then capture pano_360' },
    ])
    expect(fanoutFor('sweep', {}, [1, 2, 4])[2].cmd).toBe('lane 3 of 3, lawnmower pattern')
  })
})

describe('chips', () => {
  test('carry flight state and battery, and name the first readiness reason when not selectable', () => {
    const chips = aircraftChips(connected([1]))
    expect(chips[0]).toMatchObject({ id: 'D-01', sub: 'hovering · 78%', selected: true, selectable: true, reason: '' })
    expect(chips[2]).toMatchObject({ id: 'D-03', selectable: false, reason: 'telemetry stale' })
    expect(chipBlockers(connected([1]))).toBe('D-03 telemetry stale')
  })
})

describe('capture readiness', () => {
  test('the gate sentence follows the design order and stays honest without guidance', () => {
    expect(captureGate(createInitialControlState(session, t), 'room-01', true, null).text).toBe(
      'The console connection is disconnected. Capture room cannot be sent.',
    )
    expect(captureGate(connected([], { estop: true }), 'room-01', true, null).text).toBe(
      'The network stop is active. Capture room is refused until the relay reports it clear.',
    )
    expect(captureGate(connected([1, 2]), 'room-01', true, null).text).toBe(
      'capture_room needs exactly one aircraft selected. 2 selected.',
    )
    expect(captureGate({ ...connected([1]), selection: [3] }, 'room-01', true, null).text).toBe(
      'D-03 is not ready: telemetry_stale, camera_not_ready.',
    )
    expect(captureGate(connected([1]), 'Kitchen', false, null).text).toBe(
      'The room identifier must be lower-case letters, digits and hyphens, 3 to 24 characters.',
    )
    expect(captureGate(connected([2]), 'room-01', true, null).text).toBe('D-02 does not advertise pano_360.')
    expect(captureGate(connected([1]), 'room-01', true, { ...guidance, motion_ok: false })).toEqual({
      ready: false,
      text: 'The motion gate fails: the aircraft is still moving. Hold it, then capture.',
    })
    expect(captureGate(connected([1]), 'room-01', true, guidance)).toEqual({
      ready: true,
      text: 'All gates pass. D-01 will capture pano_360 in room-01.',
    })
    expect(captureGate(connected([1]), 'room-01', true, null)).toEqual({
      ready: true,
      text: 'Gates unreported. D-01 will capture pano_360 in room-01; the arbiter checks readiness before dispatch.',
    })
  })

  test('the three-step flow reports what is done, current, and blocking', () => {
    const flow = captureFlow(connected([1]), 'room-01', true, guidance)
    expect(flow.map((step) => [step.done, step.current, step.state])).toEqual([
      [true, false, 'D-01 selected, hovering and ready'],
      [true, false, 'room-01 · pano_360'],
      [false, true, 'all six gates pass'],
    ])
    const blocked = captureFlow(connected([1, 2]), '', false, { ...guidance, storage_ok: false, motion_ok: false })
    expect(blocked[0].state).toBe('2 selected — capture_room takes exactly one')
    expect(blocked[1].state).toBe('room identifier needed')
    expect(blocked[2]).toMatchObject({ state: 'gates blocking: storage, motion', tone: 'danger' })
    expect(captureFlow(connected([]), 'room-01', true, null)[2]).toMatchObject({ state: 'gates unreported', tone: 'muted' })
  })

  test('gates, sectors and notes render both guidance modes and the unreported state', () => {
    expect(gateRows(guidance).map((gate) => gate.word)).toEqual(['pass', 'pass', 'pass', 'pass', 'pass', 'pass'])
    expect(gateRows({ ...guidance, camera_ok: false })[2]).toEqual({ key: 'camera', word: 'fail', tone: 'danger' })
    expect(gateRows(null).every((gate) => gate.word === 'unreported' && gate.tone === 'muted')).toBe(true)
    expect(compassSectors(guidance).map((sector) => sector.coverage)).toEqual(guidance.coverage)
    expect(compassSectors(guidance)[3].rotation).toBe('translateX(-50%) rotate(135deg)')
    expect(compassSectors(null).every((sector) => sector.coverage === 'unreported')).toBe(true)
    expect(sectorSummary(guidance)).toBe('2 unseen, 2 weak, 4 accepted')
    expect(sectorSummary(null)).toBe('coverage unreported')
    expect(guidanceNote(guidance)).toBe(
      'visual_advisory: guidance suggests yaw and gimbal only. No XYZ move is suggested in this mode.',
    )
    expect(guidanceNote({ ...guidance, guidance_mode: 'registered_metric' })).toBe(
      'registered_metric: metric moves are available.',
    )
    expect(guidanceNote(null)).toMatch(/unreported/)
  })
})

describe('requests', () => {
  test('tones and retry gating', () => {
    expect(requestTone('completed')).toBe('ok')
    expect(requestTone('refused')).toBe('danger')
    expect(requestTone('invalidated')).toBe('warn')
    expect(requestTone('sent')).toBe('ink')

    const state = connected([1])
    const deps = { now: () => t, nextId: () => 'intent-r' }
    const hold = createRequestRecord(
      createIntent({ name: 'hold', args: {}, selection: [1], source: 'console', session }, deps),
      t,
    )
    expect(retryBlockedReason(hold, state)).toBeNull()
    expect(retryBlockedReason(hold, createInitialControlState(session, t))).toBe(
      'Disabled: the console connection is disconnected.',
    )
    const staleHold = { ...hold, intent: { ...hold.intent, selection: [3] } }
    expect(retryBlockedReason(staleHold, state)).toBe(
      'Disabled: D-03 is no longer ready. No substitute aircraft is selected.',
    )
    expect(
      retryBlockedReason(
        hold,
        connected([1], { capability_profile: 'land-only', enabled_intent_names: ['land'] }),
      ),
    ).toBe('Disabled: hold is disabled by relay capability profile land-only.')
    const landAll = createRequestRecord(
      createIntent({ name: 'land_all', args: {}, selection: [1, 2, 3, 4], source: 'console', session }, deps),
      t,
    )
    expect(retryBlockedReason(landAll, state)).toBeNull()
  })
})

describe('mission steps', () => {
  test('ten Appendix E steps show the integrated capability status', () => {
    expect(MISSION_STEPS).toHaveLength(10)
    expect(MISSION_STEPS.map((step) => [step.intent, step.status])).toEqual([
      ['arm', 'available'],
      ['select', 'available'],
      ['takeoff', 'available'],
      ['confirm', 'available'],
      ['formation_set', 'available'],
      ['translate', 'available'],
      ['altitude', 'available'],
      ['sweep', 'available'],
      ['come_home', 'available'],
      ['land_all', 'available'],
    ])
  })
})
