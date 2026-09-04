import { describe, expect, test } from 'vitest'
import {
  createInitialControlState,
  createRequestRecord,
  type ControlState,
  type OperatorNotice,
  type RequestRecord,
} from '../control/state'
import type { IntentV1, RelayAircraftState } from '../relay/contract'
import {
  deriveInvalidation,
  deriveLinks,
  deriveRcLine,
  deriveReadyCount,
  deriveSelectionLabel,
  deriveStateTags,
  deriveStop,
  newestAdvisory,
  newestDanger,
} from './derive'
import { formatTime } from './format'

const t = 1_756_700_000_000

function drone(overrides: Partial<RelayAircraftState> = {}): RelayAircraftState {
  return {
    drone_id: 1,
    connection_epoch: 1,
    membership: 'ready',
    readiness_reasons: [],
    flight_state: 'hovering',
    battery: 0.8,
    link: 0.9,
    pos_quality: 0.92,
    control_authority: true,
    rc_safety_operator_present: true,
    last_seen_at: t,
    camera_patterns: ['pano_360'],
    selectable: true,
    adapter_id: 'adapter-1',
    adapter_capabilities: ['flight'],
    home_pose: null,
    telemetry: null,
    membership_history: [],
    ...overrides,
  }
}

function connected(overrides: Partial<ControlState> = {}): ControlState {
  const base = createInitialControlState('derive-session', t)
  return {
    ...base,
    connection: { status: 'connected', transport: 'fixture', changedAt: t },
    keyboardConnection: { status: 'connected', transport: 'fixture', changedAt: t },
    aircraft: {
      1: drone(),
      2: drone({ drone_id: 2, control_authority: false, rc_safety_operator_present: false }),
      3: drone({ drone_id: 3, membership: 'degraded', selectable: false }),
    },
    selection: [1],
    armed: true,
    ...overrides,
  }
}

function intent(id: string): IntentV1 {
  return {
    v: 1,
    t,
    type: 'intent',
    intent_id: id,
    retry_of: null,
    source: 'console',
    session: 'derive-session',
    name: 'hold',
    args: {},
    selection: [1],
    mode: 'indoor',
    confirm: false,
  }
}

describe('network stop derivation', () => {
  const noTimes = { seenActiveAt: null, seenClearedAt: null }

  test('connected and clear', () => {
    const stop = deriveStop(connected(), noTimes, t)
    expect(stop).toMatchObject({
      title: 'Network stop',
      sub: 'estop · Shift+Escape',
      active: false,
      disabled: false,
      reason: 'Sends estop to every aircraft in the roster.',
    })
  })

  test('degraded counts as connected for the stop', () => {
    const state = connected({ connection: { status: 'degraded', transport: 'websocket', changedAt: t } })
    expect(deriveStop(state, noTimes, t).disabled).toBe(false)
  })

  test('disconnected carries the relay reason and stays disabled', () => {
    const state = connected({
      connection: { status: 'disconnected', transport: 'websocket', changedAt: t, reason: 'Socket closed.' },
    })
    const stop = deriveStop(state, noTimes, t)
    expect(stop.disabled).toBe(true)
    expect(stop.reason).toBe(
      'Disabled: the console socket is disconnected. Socket closed. Use the physical RC or Shift+Escape on the keyboard connection.',
    )
  })

  test('active with and without a seen time', () => {
    const state = connected({ estop: true })
    expect(deriveStop(state, noTimes, t)).toMatchObject({
      title: 'Stop active',
      sub: 'active · Shift+Escape',
      active: true,
      disabled: false,
    })
    expect(deriveStop(state, { seenActiveAt: t, seenClearedAt: null }, t).sub).toBe(
      `seen ${formatTime(t)} · Shift+Escape`,
    )
    expect(deriveStop(state, noTimes, t).reason).toMatch(/Pressing again re-sends estop/)
  })

  test('cleared notice lasts ten seconds', () => {
    const times = { seenActiveAt: null, seenClearedAt: t }
    expect(deriveStop(connected(), times, t + 9_999).reason).toBe(
      `Stop cleared, seen ${formatTime(t)}, reported by the relay.`,
    )
    expect(deriveStop(connected(), times, t + 10_000).reason).toBe(
      'Sends estop to every aircraft in the roster.',
    )
  })
})

describe('header derivations', () => {
  test('state tags follow armed and estop', () => {
    expect(deriveStateTags(connected()).map((tag) => tag.label)).toEqual(['Armed', 'Stop clear', 'indoor'])
    expect(deriveStateTags(connected({ armed: false, estop: true })).map((tag) => tag.variant)).toEqual([
      'disarmed',
      'stop-active',
      'mode',
    ])
  })

  test('selection label and ready count', () => {
    expect(deriveSelectionLabel([1, 2])).toBe('D-01  D-02')
    expect(deriveSelectionLabel([])).toBe('none selected')
    expect(deriveReadyCount(connected().aircraft)).toBe('2 of 3 ready')
    expect(deriveReadyCount({})).toBe('0 of 0 ready')
  })

  test('RC line reports authority per selected aircraft and turns danger on takeover or absence', () => {
    expect(deriveRcLine(connected())).toEqual({
      text: 'D-01 Sweep · RC operator present',
      danger: false,
    })
    expect(deriveRcLine(connected({ selection: [1, 2] }))).toEqual({
      text: 'D-01 Sweep · RC operator present   D-02 RC takeover · RC operator absent',
      danger: true,
    })
    expect(deriveRcLine(connected({ selection: [] })).text).toBe('D-01 Sweep · RC operator present')
    expect(deriveRcLine(connected({ selection: [], aircraft: {} }))).toEqual({
      text: 'no aircraft reported',
      danger: false,
    })
  })

  test('link pills include the webcam only when a status is present', () => {
    const state = connected({
      keyboardConnection: { status: 'degraded', transport: 'websocket', changedAt: t },
    })
    expect(deriveLinks(state).map((link) => [link.short, link.value, link.tone])).toEqual([
      ['relay', 'connected', 'ok'],
      ['keys', 'degraded', 'warn'],
    ])
    expect(deriveLinks(state, 'disconnected').at(-1)).toMatchObject({
      short: 'webcam',
      value: 'disconnected',
      tone: 'danger',
    })
  })
})

describe('invalidation line', () => {
  function invalidated(id: string, sent: boolean): RequestRecord {
    const record = createRequestRecord(intent(id), t)
    return {
      ...record,
      status: 'invalidated',
      reasonCode: 'stale_roster',
      detail: 'Roster changed.',
      timestamps: sent ? { draft: t, sent: t + 1, invalidated: t + 2 } : { draft: t, invalidated: t + 2 },
    }
  }

  test('shows only for the newest never-sent invalidation with nothing pending', () => {
    expect(deriveInvalidation([invalidated('a', false)], null)).toEqual({
      reasonCode: 'stale_roster',
      detail: 'Roster changed.',
    })
    expect(deriveInvalidation([invalidated('a', true)], null)).toBeNull()
    expect(deriveInvalidation([createRequestRecord(intent('b'), t), invalidated('a', false)], null)).toBeNull()
    const pending: RequestRecord = { ...createRequestRecord(intent('c'), t), status: 'pending_confirmation' }
    expect(deriveInvalidation([invalidated('a', false)], pending)).toBeNull()
  })
})

describe('header notices', () => {
  const notice = (id: string, level: OperatorNotice['level']): OperatorNotice => ({
    id,
    level,
    title: `${level} ${id}`,
    detail: 'detail',
    t,
  })

  test('the banner takes the newest danger and the line the newest warning or info', () => {
    const notices = [
      notice('d2', 'danger'),
      notice('w1', 'warning'),
      notice('i1', 'info'),
      notice('d1', 'danger'),
    ]
    expect(newestDanger(notices)?.id).toBe('d2')
    expect(newestAdvisory(notices)?.id).toBe('w1')
    expect(newestAdvisory([notice('i1', 'info'), notice('w1', 'warning')])?.id).toBe('i1')
  })

  test('nothing surfaces when only danger notices, or none, are kept', () => {
    expect(newestAdvisory([])).toBeNull()
    expect(newestAdvisory([notice('d1', 'danger')])).toBeNull()
    expect(newestDanger([notice('w1', 'warning')])).toBeNull()
  })
})
