import { describe, expect, test } from 'vitest'
import {
  CONSOLE_INTENT_NAMES,
  C1_BASIC_CONTROL_INTENTS,
  isConsoleIntentV1,
  requiresConfirmation,
  selectionRule,
  type ConsoleIntentName,
  type IntentArgs,
  type IntentV1,
} from '../relay/contract'
import {
  clampTranslateSteps,
  confirmIntent,
  createCaptureArgs,
  createIntent,
  createTranslateArgs,
  isValidRoomId,
  retryIntent,
} from './intent'
import { buildPlanPreview, planSteps, planTitle } from './plan'

const session = 'session-intent-test'
const t = 1_756_700_000_000
const deps = { now: () => t, nextId: () => 'intent-1' }

/** The exact args each control sends, per the relay's _parse_args. */
const ENVELOPES: Record<ConsoleIntentName, { args: IntentArgs; selection: number[] }> = {
  arm: { args: {}, selection: [1] },
  disarm: { args: {}, selection: [1] },
  estop: { args: {}, selection: [] },
  select: { args: { ids: [1, 2] }, selection: [1] },
  takeoff: { args: {}, selection: [1, 2] },
  land: { args: {}, selection: [1] },
  land_all: { args: {}, selection: [1, 2, 3, 4] },
  hold: { args: {}, selection: [1] },
  translate: { args: { dx: 2, dy: 0 }, selection: [1] },
  altitude: { args: { delta: 1 }, selection: [1] },
  formation_next: { args: {}, selection: [1] },
  formation_set: { args: { name: 'circle' }, selection: [1, 2] },
  spacing: { args: { delta: -1 }, selection: [1] },
  come_home: { args: {}, selection: [1] },
  sweep: { args: {}, selection: [1] },
  capture_room: {
    args: { room_id: 'kitchen-01', capture_id: 'capture-intent-1', pattern: 'pano_360' },
    selection: [1],
  },
  navigate: { args: { zone_id: 'atrium' }, selection: [1] },
  search: { args: { zone_id: 'atrium', target_class: 'backpack' }, selection: [1] },
}

describe('intent envelopes', () => {
  test('navigation remains unavailable in the default capability profile', () => {
    expect(CONSOLE_INTENT_NAMES).toContain('navigate')
    expect(C1_BASIC_CONTROL_INTENTS).not.toContain('navigate')
    expect(requiresConfirmation('navigate')).toBe(true)
  })
  test.each(CONSOLE_INTENT_NAMES)('%s builds a conformant Intent v1 envelope', (name) => {
    const { args, selection } = ENVELOPES[name]
    const draft = createIntent({ name, args, selection, source: 'console', session }, deps)
    expect(draft).toMatchObject({
      v: 1,
      t,
      type: 'intent',
      intent_id: 'intent-1',
      retry_of: null,
      source: 'console',
      session,
      name,
      args,
      selection,
      mode: 'indoor',
      confirm: false,
    })
    const wire = requiresConfirmation(name) ? confirmIntent(draft, t + 5) : draft
    expect(isConsoleIntentV1(wire)).toBe(true)
  })

  test('navigation preview describes the frozen route arrival hold', () => {
    const preview = buildPlanPreview(
      createIntent({ name: 'navigate', args: { zone_id: 'atrium' }, selection: [1], source: 'console', session }, deps),
      4,
      { map_pin: ['map', 'v1'], geometry_pin: ['geometry', 'v1'], configuration_id: 'nav-v1', floor_id: 'level_1', catalog_version: 'catalog-v1', zones: [] },
    )
    expect(preview.steps.join(' ')).toContain('arrival slot')
    expect(preview.navigationKey).toContain('catalog-v1')
  })

  test.each(['takeoff', 'land', 'land_all', 'sweep', 'capture_room', 'navigate'] as const)(
    '%s requires confirmation and is refused locally without it',
    (name) => {
      expect(requiresConfirmation(name)).toBe(true)
      const { args, selection } = ENVELOPES[name]
      const draft = createIntent<ConsoleIntentName>({ name, args, selection, source: 'console', session }, deps)
      expect(isConsoleIntentV1(draft)).toBe(false)
      expect(isConsoleIntentV1(confirmIntent(draft, t + 1))).toBe(true)
    },
  )

  test.each([
    'arm',
    'disarm',
    'estop',
    'select',
    'hold',
    'translate',
    'altitude',
    'formation_next',
    'formation_set',
    'spacing',
    'come_home',
  ] as const)('%s sends without confirmation', (name) => {
    expect(requiresConfirmation(name)).toBe(false)
    const { args, selection } = ENVELOPES[name]
    expect(
      isConsoleIntentV1(
        createIntent<ConsoleIntentName>({ name, args, selection, source: 'console', session }, deps),
      ),
    ).toBe(true)
  })

  test('formation_next takes no args and translate carries dx and dy only', () => {
    const base = createIntent(
      { name: 'formation_next', args: {}, selection: [1], source: 'console', session },
      deps,
    )
    expect(isConsoleIntentV1(base)).toBe(true)
    expect(
      isConsoleIntentV1({ ...base, args: { name: 'line' } as unknown as IntentArgs }),
    ).toBe(false)

    const translate = createIntent(
      { name: 'translate', args: createTranslateArgs('north', 2), selection: [1], source: 'console', session },
      deps,
    )
    expect(translate.args).toEqual({ dx: 0, dy: 2 })
    expect(isConsoleIntentV1(translate)).toBe(true)
    expect(
      isConsoleIntentV1({ ...translate, args: { dx: 1 } as unknown as IntentArgs }),
    ).toBe(false)
  })

  test('selection rules follow the brief', () => {
    expect(selectionRule('arm')).toBe('any')
    expect(selectionRule('land_all')).toBe('all')
    expect(selectionRule('capture_room')).toBe('exactly one')
    expect(selectionRule('estop')).toBe('fleet')

    const hold = createIntent({ name: 'hold', args: {}, selection: [], source: 'console', session }, deps)
    expect(isConsoleIntentV1(hold)).toBe(false)
    const armEmpty = createIntent({ name: 'arm', args: {}, selection: [], source: 'console', session }, deps)
    expect(isConsoleIntentV1(armEmpty)).toBe(true)
    const stopWithSelection = createIntent(
      { name: 'estop', args: {}, selection: [1], source: 'keyboard', session },
      deps,
    )
    expect(isConsoleIntentV1(stopWithSelection)).toBe(false)
    const captureTwo = confirmIntent(
      createIntent<ConsoleIntentName>(
        { name: 'capture_room', args: ENVELOPES.capture_room.args, selection: [1, 2], source: 'console', session },
        deps,
      ),
      t,
    )
    expect(isConsoleIntentV1(captureTwo)).toBe(false)
  })

  test('rejects args the relay would refuse as invalid_payload', () => {
    const spacing = createIntent(
      { name: 'spacing', args: { delta: 1 }, selection: [1], source: 'console', session },
      deps,
    )
    expect(isConsoleIntentV1({ ...spacing, args: { delta: Number.NaN } })).toBe(false)
    const formation = createIntent(
      { name: 'formation_set', args: { name: 'grid' }, selection: [1], source: 'console', session },
      deps,
    )
    expect(isConsoleIntentV1({ ...formation, args: { name: '' } as unknown as IntentArgs })).toBe(false)
    expect(isConsoleIntentV1({ ...formation, name: 'survey_area' as unknown as ConsoleIntentName })).toBe(false)
  })
})

describe('translate helpers', () => {
  test('clamps steps to the 1 to 6 range and scales the unit vector', () => {
    expect(clampTranslateSteps(0)).toBe(1)
    expect(clampTranslateSteps(9)).toBe(6)
    expect(clampTranslateSteps(Number.NaN)).toBe(1)
    expect(createTranslateArgs('west', 3)).toEqual({ dx: -3, dy: 0 })
    expect(createTranslateArgs('south', 1)).toEqual({ dx: 0, dy: -1 })
    expect(createTranslateArgs('east', 12)).toEqual({ dx: 6, dy: 0 })
  })
})

describe('capture helpers', () => {
  test('validates the room identifier rule from the design', () => {
    expect(isValidRoomId('kitchen-01')).toBe(true)
    expect(isValidRoomId('abc')).toBe(true)
    expect(isValidRoomId('ab')).toBe(false)
    expect(isValidRoomId('-kitchen')).toBe(false)
    expect(isValidRoomId('Kitchen-01')).toBe(false)
    expect(isValidRoomId('a'.repeat(25))).toBe(false)
  })

  test('mints the capture id from the intent id', () => {
    expect(createCaptureArgs('kitchen-01', 'intent-9', 'reconstruct_8')).toEqual({
      room_id: 'kitchen-01',
      capture_id: 'capture-intent-9',
      pattern: 'reconstruct_8',
    })
  })
})

describe('retry', () => {
  test('mints a new id, links retry_of, and copies args and selection', () => {
    const original: IntentV1 = createIntent(
      { name: 'translate', args: { dx: 2, dy: 0 }, selection: [1, 2], source: 'console', session },
      deps,
    )
    const retry = retryIntent(original, { now: () => t + 50, nextId: () => 'intent-2' })
    expect(retry).toMatchObject({
      intent_id: 'intent-2',
      retry_of: 'intent-1',
      name: 'translate',
      args: { dx: 2, dy: 0 },
      selection: [1, 2],
      confirm: false,
      t: t + 50,
    })
    expect(retry.args).not.toBe(original.args)
    expect(retry.selection).not.toBe(original.selection)
    expect(isConsoleIntentV1(retry)).toBe(true)
  })

  test('keeps the confirmation of a confirmation-gated request so the retry sends without a second preview', () => {
    const original: IntentV1 = confirmIntent(
      createIntent(
        {
          name: 'capture_room',
          args: createCaptureArgs('kitchen-01', 'intent-1', 'pano_360'),
          selection: [1],
          source: 'console',
          session,
        },
        deps,
      ),
      t + 10,
    )
    const retry = retryIntent(original, { now: () => t + 50, nextId: () => 'intent-2' })
    expect(retry).toMatchObject({
      intent_id: 'intent-2',
      retry_of: 'intent-1',
      name: 'capture_room',
      args: { room_id: 'kitchen-01', capture_id: 'capture-intent-1', pattern: 'pano_360' },
      selection: [1],
      confirm: true,
      t: t + 50,
    })
    expect(isConsoleIntentV1(retry)).toBe(true)
  })
})

describe('plan preview', () => {
  test('titles and steps follow the design for confirmation-gated intents', () => {
    const takeoff = createIntent(
      { name: 'takeoff', args: {}, selection: [1, 2], source: 'console', session },
      deps,
    )
    expect(planTitle(takeoff)).toBe('Takeoff')
    expect(planSteps(takeoff)).toEqual([
      'Confirm D-01, D-02 is armed and ready.',
      'Take off to the indoor hover altitude.',
      'Hold and report hovering.',
    ])

    const landAll = createIntent(
      { name: 'land_all', args: {}, selection: [1, 2, 3, 4], source: 'console', session },
      deps,
    )
    expect(planTitle(landAll)).toBe('Land all fleet')
    expect(planSteps(landAll)).toHaveLength(3)

    const capture = createIntent(
      {
        name: 'capture_room',
        args: createCaptureArgs('kitchen-01', 'intent-1', 'pano_360'),
        selection: [1],
        source: 'console',
        session,
      },
      deps,
    )
    expect(buildPlanPreview(capture, 9)).toEqual({
      title: 'Capture room',
      rosterVersion: 9,
      steps: [
        'Hold D-01 at its current pose and confirm the motion gate is clear.',
        'Capture pano_360 in room kitchen-01 as capture capture-intent-1.',
        'Produce one full_equirectangular set.',
        'Download the file set to the ground station and record checksums and pose metadata.',
      ],
    })

    const sweep = createIntent({ name: 'sweep', args: {}, selection: [1], source: 'console', session }, deps)
    expect(planTitle(sweep)).toBe('Sweep area')
    expect(planSteps(sweep)).toEqual([
      'Assign one deterministic lawnmower lane per aircraft inside a box derived from the authoritative aircraft positions and spacing.',
      'Refuse before dispatch if the requested box or any lane leaves the configured geofence.',
      'Send the frozen lanes to D-01.',
    ])
  })
})
