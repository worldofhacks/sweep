import { describe, expect, it, vi } from 'vitest'
import {
  MAX_NAVIGATION_ALIASES, MAX_NAVIGATION_CONFIG_BYTES, MAX_NAVIGATION_COORDINATE_M,
  MAX_NAVIGATION_DESTINATIONS, MAX_NAVIGATION_TARGETS, MAX_NAVIGATION_WAYPOINTS,
  NAVIGATION_CONFIRMATION_UNAVAILABLE, NAVIGATION_UNAVAILABLE, UnavailableNavigationClient,
  navigationCatalogValidity, navigationPreviewValidity, parseNavigationCatalog,
  parseNavigationPreview, resolveNavigationDestination, resolveNavigationZoneId,
} from './index'
import type {
  NavigationCatalog, NavigationContext, NavigationDestination, NavigationPreview,
  NavigationPreviewRequest, NavigationRoute, NavigationTarget,
} from './index'

const NOW = 2000
const selected: NavigationTarget[] = [
  { id: 5, deviceClass: 'aircraft', epoch: 2 },
  { id: 11, deviceClass: 'ground_vehicle', epoch: 3 },
]

function catalog(): NavigationCatalog {
  return {
    session: 'fixture-session', catalogVersion: 'catalog-1', receivedAt: 1000, expiresAt: 10000,
    map: {
      mapId: 'fixture-map', floorId: 'floor-1', frame: 'world', accepted: true, approvalId: 'approval-1',
      mapPin: { version: 'map-1', contentSha256: 'a'.repeat(64) },
      geometryPin: { version: 'geometry-1', contentSha256: 'b'.repeat(64) },
      navigationPin: { version: 'navigation-1', contentSha256: 'c'.repeat(64) },
    },
    configVersion: 'configuration-1', motionConfig: { aircraft: { speedMps: 0.5 }, ground: { speedMps: 0.3 } },
    destinations: [{
      zoneId: 'atrium', name: 'Atrium', aliases: ['Lobby', 'Main Hall'], floorId: 'floor-1',
      excluded: false, reachability: 'reachable', allowedClasses: ['aircraft', 'ground_vehicle'],
    }],
  }
}

function route(target: NavigationTarget, count = 2): NavigationRoute {
  const end = { xM: 4, yM: target.id % 100, zM: target.deviceClass === 'aircraft' ? 1 : 0, floorId: 'floor-1', frame: 'world' as const }
  return {
    target,
    waypoints: Array.from({ length: count }, (_, index) => ({ ...end, xM: 4 * index / (count - 1) })),
    arrivalSlot: { slotId: `slot-${target.id}`, zoneId: 'atrium', position: end },
    holdBehavior: target.deviceClass === 'aircraft' ? 'hover' : 'stop',
  }
}

function preview(targets: readonly NavigationTarget[] = selected, waypointCount = 2): NavigationPreview {
  const accepted = catalog()
  return {
    previewId: 'preview-1', session: accepted.session, intentId: 'intent-1', rosterVersion: 7,
    selected: targets, destination: accepted.destinations[0], map: accepted.map,
    catalogVersion: accepted.catalogVersion, configVersion: accepted.configVersion, motionConfig: accepted.motionConfig,
    routes: targets.map((target) => route(target, waypointCount)),
    outcomes: targets.map((target) => ({ target, status: 'planned', code: 'planned', detail: 'Route supplied by the test provider.' })),
    receivedAt: 1200, expiresAt: 9000, dispatchEligible: false,
  }
}

function context(): NavigationContext {
  return { session: 'fixture-session', rosterVersion: 7, selected, destinationZoneId: 'atrium', now: NOW, intentId: 'intent-1' }
}

function changed<T>(value: T, path: readonly (string | number)[], replacement: unknown): T {
  const copy = JSON.parse(JSON.stringify(value)) as T
  let target = copy as Record<string | number, unknown>
  for (const key of path.slice(0, -1)) target = target[key] as Record<string | number, unknown>
  target[path[path.length - 1]] = replacement
  return copy
}

function destinationCatalog(destination: Partial<NavigationDestination>): NavigationCatalog {
  const accepted = catalog()
  return { ...accepted, destinations: [{ ...accepted.destinations[0], ...destination }] }
}

describe('accepted map integration contract', () => {
  it('accepts a mixed-class destination without inferring execution permission', () => {
    expect(parseNavigationCatalog(catalog())).toEqual(catalog())
    expect(parseNavigationPreview(preview())).toEqual(preview())
    expect(navigationCatalogValidity(catalog(), 'fixture-session', NOW).valid).toBe(true)
    expect(navigationPreviewValidity(preview(), catalog(), context()).valid).toBe(true)
    expect(NAVIGATION_CONFIRMATION_UNAVAILABLE).toContain('execution unavailable')
  })

  it('returns detached, deeply frozen catalog and preview values', () => {
    const source = catalog()
    const parsed = parseNavigationCatalog(source)!
    expect(parsed).not.toBe(source)
    expect(parsed.map).not.toBe(source.map)
    expect(Object.isFrozen(parsed.destinations[0].aliases)).toBe(true)
    expect(Object.isFrozen(parsed.motionConfig.aircraft)).toBe(true)
    const raw = preview()
    const frozen = parseNavigationPreview(raw)!
    expect(Object.isFrozen(frozen.routes[0].waypoints[0])).toBe(true)
    expect(Reflect.set(raw.routes[0].waypoints[0], 'xM', 7)).toBe(true)
    expect(frozen.routes[0].waypoints[0].xM).toBe(0)
    expect(Reflect.set(frozen.routes[0].waypoints[0], 'xM', 7)).toBe(false)
  })

  it.each([
    [['extra'], true], [['session'], ''], [['catalogVersion'], ' version'], [['expiresAt'], 1000],
    [['receivedAt'], -1], [['expiresAt'], Number.NaN], [['map', 'accepted'], false],
    [['map', 'approvalId'], ''], [['map', 'frame'], 'building'], [['map', 'floorId'], ''],
    [['map', 'mapPin', 'contentSha256'], 'A'.repeat(64)], [['map', 'geometryPin', 'contentSha256'], 'b'.repeat(63)],
    [['map', 'navigationPin', 'contentSha256'], 'g'.repeat(64)], [['map', 'mapPin', 'version'], ''],
    [['motionConfig'], {}], [['motionConfig'], []], [['motionConfig', 'bad'], Number.POSITIVE_INFINITY],
    [['destinations', 0, 'name'], 'atrium\nother'], [['destinations', 0, 'aliases'], ['Lobby', ' lobby ']],
    [['destinations', 0, 'aliases'], ['Lobby', 'LOBBY']], [['destinations', 0, 'allowedClasses'], ['aircraft', 'aircraft']],
    [['destinations', 0, 'allowedClasses'], ['rover']], [['destinations', 0, 'reachability'], 'maybe'],
    [['destinations', 0, 'reachability'], ['reachable']],
  ] as const)('rejects invalid catalog field %j', (path, replacement) => {
    expect(parseNavigationCatalog(changed(catalog(), path, replacement))).toBeNull()
  })

  it('rejects missing fields, cycles, accessors, hidden fields and non-JSON values', () => {
    const missing = { ...catalog() } as Record<string, unknown>
    delete missing.map
    expect(parseNavigationCatalog(missing)).toBeNull()
    const cyclic: Record<string, unknown> = {}
    cyclic.child = cyclic
    expect(parseNavigationCatalog({ ...catalog(), motionConfig: cyclic })).toBeNull()
    const getter = vi.fn(() => 'fixture-session')
    expect(parseNavigationCatalog(Object.defineProperty(catalog(), 'session', { get: getter }))).toBeNull()
    expect(getter).not.toHaveBeenCalled()
    expect(parseNavigationCatalog(Object.defineProperty(catalog(), 'hidden', { value: true }))).toBeNull()
    expect(parseNavigationCatalog({ ...catalog(), [Symbol('hidden')]: true })).toBeNull()
    expect(parseNavigationCatalog(changed(catalog(), ['motionConfig', 'date'], new Date()))).toBeNull()
    expect(parseNavigationCatalog(changed(catalog(), ['motionConfig', 'undefined'], undefined))).toBeNull()
    const sparse = [1, 2]
    Reflect.deleteProperty(sparse, '1')
    Reflect.set(sparse, 'extra', 3)
    expect(parseNavigationCatalog(changed(catalog(), ['motionConfig', 'array'], sparse))).toBeNull()
  })

  it('enforces destination, alias and UTF-8 motion configuration bounds', () => {
    const accepted = catalog()
    const destinations = Array.from({ length: MAX_NAVIGATION_DESTINATIONS }, (_, index) => ({ ...accepted.destinations[0], zoneId: `zone-${index}` }))
    expect(parseNavigationCatalog({ ...accepted, destinations })).not.toBeNull()
    expect(parseNavigationCatalog({ ...accepted, destinations: [...destinations, { ...destinations[0], zoneId: 'over-limit' }] })).toBeNull()
    expect(parseNavigationCatalog({ ...accepted, destinations: [destinations[0], destinations[0]] })).toBeNull()
    const aliases = Array.from({ length: MAX_NAVIGATION_ALIASES }, (_, index) => `alias-${index}`)
    expect(parseNavigationCatalog(destinationCatalog({ aliases }))).not.toBeNull()
    expect(parseNavigationCatalog(destinationCatalog({ aliases: [...aliases, 'over-limit'] }))).toBeNull()
    const exact = { text: 'x'.repeat(MAX_NAVIGATION_CONFIG_BYTES - JSON.stringify({ text: '' }).length) }
    expect(parseNavigationCatalog({ ...accepted, motionConfig: exact })).not.toBeNull()
    expect(parseNavigationCatalog({ ...accepted, motionConfig: { text: exact.text + 'x' } })).toBeNull()
    expect(parseNavigationCatalog({ ...accepted, motionConfig: { text: '界'.repeat(MAX_NAVIGATION_CONFIG_BYTES / 2) } })).toBeNull()
    let deep: object = { value: 1 }
    for (let index = 0; index < 7; index += 1) deep = { nested: deep }
    expect(parseNavigationCatalog({ ...accepted, motionConfig: deep })).toBeNull()
  })
})

describe('exact selected nodes and class-specific routes', () => {
  it.each([
    [['selected', 0, 'id'], 0], [['selected', 0, 'id'], -1], [['selected', 0, 'id'], 2 ** 31],
    [['selected', 0, 'id'], true], [['selected', 0, 'epoch'], 0], [['selected', 0, 'epoch'], 1.5],
    [['selected', 0, 'deviceClass'], 'robot'], [['rosterVersion'], -1], [['selected'], []],
    [['outcomes'], []], [['outcomes', 0, 'status'], ['planned']],
    [['routes', 0, 'target', 'epoch'], 9], [['outcomes', 0, 'target', 'id'], 99],
    [['routes', 0, 'holdBehavior'], 'stop'], [['routes', 1, 'holdBehavior'], 'hover'],
    [['routes', 0, 'arrivalSlot', 'zoneId'], 'other-zone'], [['routes', 0, 'arrivalSlot', 'position', 'xM'], 100],
    [['routes', 0, 'waypoints', 0, 'frame'], 'map_enu'], [['routes', 0, 'waypoints', 0, 'floorId'], 'other-floor'],
    [['routes', 0, 'waypoints', 0, 'xM'], Number.NaN], [['routes', 0, 'waypoints', 0, 'xM'], MAX_NAVIGATION_COORDINATE_M + 1],
    [['routes', 0, 'waypoints'], []], [['receivedAt'], 9000], [['expiresAt'], 1200], [['dispatchEligible'], 'true'],
  ] as const)('rejects malformed or rebound preview field %j', (path, replacement) => {
    expect(parseNavigationPreview(changed(preview(), path, replacement))).toBeNull()
  })

  it('refuses duplicates, omissions and widening across targets, routes and outcomes', () => {
    const original = preview()
    expect(parseNavigationPreview({ ...original, selected: [selected[0], selected[0]] })).toBeNull()
    expect(parseNavigationPreview({ ...original, routes: [original.routes[0], original.routes[0]] })).toBeNull()
    expect(parseNavigationPreview({ ...original, outcomes: [original.outcomes[0], original.outcomes[0]] })).toBeNull()
    expect(parseNavigationPreview({ ...original, routes: [original.routes[0]] })).toBeNull()
    expect(parseNavigationPreview(changed(original, ['routes', 1, 'arrivalSlot', 'slotId'], 'slot-5'))).toBeNull()
    expect(parseNavigationPreview({ ...original, selected: [selected[0]] })).toBeNull()
    expect(parseNavigationPreview({ ...original, routes: [...original.routes, route({ id: 99, deviceClass: 'aircraft', epoch: 1 })] })).toBeNull()
  })

  it('accepts the configured positive Int and 64-node limits without increasing route bounds', () => {
    const maximumId = { id: 2 ** 31 - 1, deviceClass: 'aircraft' as const, epoch: Number.MAX_SAFE_INTEGER }
    expect(parseNavigationPreview(preview([maximumId]))).not.toBeNull()
    const targets = Array.from({ length: MAX_NAVIGATION_TARGETS }, (_, index) => ({ id: index + 1, deviceClass: 'aircraft' as const, epoch: 1 }))
    expect(parseNavigationPreview(preview(targets))).not.toBeNull()
    expect(parseNavigationPreview(preview([...targets, { ...targets[0], id: 65 }]))).toBeNull()
    expect(parseNavigationPreview(preview([selected[0]], MAX_NAVIGATION_WAYPOINTS))).not.toBeNull()
    expect(parseNavigationPreview(preview([selected[0]], MAX_NAVIGATION_WAYPOINTS + 1))).toBeNull()
    expect(parseNavigationPreview(preview(targets.slice(0, 16), MAX_NAVIGATION_WAYPOINTS))).not.toBeNull()
    expect(parseNavigationPreview(preview(targets.slice(0, 17), MAX_NAVIGATION_WAYPOINTS))).toBeNull()
  })

  it('preserves exact typed per-node refusals without inventing a route', () => {
    const original = preview()
    const refused: NavigationPreview = {
      ...original, routes: [original.routes[0]], outcomes: [original.outcomes[0], {
        target: selected[1], status: 'refused', code: 'ground_navigation_unsupported', detail: 'The current ground adapter does not advertise navigation.',
      }],
    }
    expect(parseNavigationPreview(refused)).toEqual(refused)
    expect(navigationPreviewValidity(refused, catalog(), context()).code).toBe('node_refused')
    expect(parseNavigationPreview({ ...refused, dispatchEligible: true })).toBeNull()
  })
})

describe('canonical destination names and aliases', () => {
  it.each(['atrium', 'Atrium', 'lObBy', '  MAIN   HALL ', 'Ａｔｒｉｕｍ'])('resolves %s only against the current accepted catalog', (name) => {
    expect(resolveNavigationDestination(catalog(), name, NOW, ['aircraft', 'ground_vehicle'])).toMatchObject({ kind: 'resolved', destination: { zoneId: 'atrium' } })
  })

  it('clarifies ambiguous aliases without preferring an eligible or first destination', () => {
    const accepted = catalog()
    const another = { ...accepted.destinations[0], zoneId: 'west-lobby', name: 'West Lobby', excluded: true }
    const ambiguous = resolveNavigationDestination({ ...accepted, destinations: [...accepted.destinations, another] }, 'Lobby', NOW)
    expect(ambiguous.kind).toBe('ambiguous')
    if (ambiguous.kind === 'ambiguous') expect(ambiguous.candidates.map((item) => item.zoneId)).toEqual(['atrium', 'west-lobby'])
    expect(resolveNavigationDestination({ ...accepted, destinations: [...accepted.destinations, another] }, 'west-lobby', NOW)).toMatchObject({ kind: 'refused', code: 'destination_excluded' })
  })

  it('keeps explicit canonical selection distinct from ambiguous free-text aliases', () => {
    const accepted = catalog()
    const withCollision = { ...accepted, destinations: [...accepted.destinations, { ...accepted.destinations[0], zoneId: 'west', name: 'West', aliases: ['atrium'] }] }
    expect(resolveNavigationDestination(withCollision, 'atrium', NOW).kind).toBe('ambiguous')
    expect(resolveNavigationZoneId(withCollision, 'atrium', NOW)).toMatchObject({ kind: 'resolved', destination: { zoneId: 'atrium' } })
    expect(resolveNavigationZoneId(withCollision, 'Lobby', NOW)).toMatchObject({ kind: 'refused', code: 'destination_unknown' })
    expect(resolveNavigationZoneId(withCollision, 'ATRIUM', NOW)).toMatchObject({ kind: 'refused', code: 'destination_unknown' })
    expect(resolveNavigationZoneId(destinationCatalog({ excluded: true }), 'atrium', NOW)).toMatchObject({ kind: 'refused', code: 'destination_excluded' })
  })

  it.each([
    [{ excluded: true }, 'destination_excluded'], [{ floorId: 'floor-2' }, 'wrong_floor'],
    [{ reachability: 'unknown' }, 'destination_unreachable'], [{ reachability: 'unreachable' }, 'destination_unreachable'],
    [{ allowedClasses: ['aircraft'] }, 'unsupported_selection'],
  ] as const)('refuses unavailable destination evidence %j', (changes, code) => {
    expect(resolveNavigationDestination(destinationCatalog(changes), 'atrium', NOW, ['ground_vehicle'])).toMatchObject({ kind: 'refused', code })
  })

  it('refuses missing, stale and unknown destinations without coordinate or name fallbacks', () => {
    expect(resolveNavigationDestination(null, 'Lobby', NOW)).toMatchObject({ kind: 'refused', code: 'catalog_unavailable' })
    expect(resolveNavigationDestination(catalog(), 'Lobby', 10000)).toMatchObject({ kind: 'refused', code: 'catalog_expired' })
    expect(resolveNavigationDestination(catalog(), 'Kitchen', NOW)).toMatchObject({ kind: 'refused', code: 'destination_unknown' })
    expect(resolveNavigationDestination(catalog(), '   ', NOW)).toMatchObject({ kind: 'refused', code: 'destination_missing' })
  })
})

describe('current-context and frozen-preview validity', () => {
  it.each([
    [{ session: 'another-session' }, 'session_changed'], [{ rosterVersion: 8 }, 'roster_changed'],
    [{ destinationZoneId: 'other' }, 'destination_changed'], [{ intentId: 'another-intent' }, 'intent_changed'],
    [{ now: 1100 }, 'preview_not_current'], [{ now: 9000 }, 'preview_expired'],
    [{ selected: [...selected].reverse() }, 'selection_changed'],
    [{ selected: [selected[0]] }, 'selection_changed'],
    [{ selected: [selected[0], { ...selected[1], epoch: 4 }] }, 'selection_changed'],
    [{ selected: [selected[0], { ...selected[1], deviceClass: 'aircraft' }] }, 'selection_changed'],
  ] as const)('invalidates current context change %j', (changes, code) => {
    expect(navigationPreviewValidity(preview(), catalog(), { ...context(), ...changes }).code).toBe(code)
  })

  it.each([
    [['catalogVersion'], 'catalog-2', 'catalog_changed'], [['map', 'mapPin', 'version'], 'map-2', 'map_changed'],
    [['map', 'geometryPin', 'contentSha256'], 'd'.repeat(64), 'map_changed'],
    [['map', 'navigationPin', 'contentSha256'], 'e'.repeat(64), 'map_changed'],
    [['map', 'approvalId'], 'approval-2', 'map_changed'], [['map', 'mapId'], 'new-map', 'map_changed'],
    [['configVersion'], 'configuration-2', 'motion_config_changed'],
    [['motionConfig', 'ground', 'speedMps'], 0.1, 'motion_config_changed'],
    [['destinations', 0, 'aliases'], ['New Lobby'], 'destination_changed'],
  ] as const)('invalidates frozen catalog input %j', (path, replacement, code) => {
    expect(navigationPreviewValidity(preview(), changed(catalog(), path, replacement), context()).code).toBe(code)
  })

  it('compares captured route, slot, intent and expiry integrity independently of map freshness', () => {
    const frozen = parseNavigationPreview(preview())!
    for (const [path, replacement] of [
      [['routes', 0, 'waypoints', 0, 'xM'], 0.25], [['routes', 0, 'arrivalSlot', 'slotId'], 'replacement-slot'],
      [['previewId'], 'replacement-preview'], [['expiresAt'], 9500], [['outcomes', 0, 'detail'], 'Changed report.'],
    ] as const) {
      const mutated = changed(frozen, path, replacement)
      expect(parseNavigationPreview(mutated)).not.toBeNull()
      expect(navigationPreviewValidity(mutated, catalog(), { ...context(), frozenPreview: frozen }).code).toBe('preview_changed')
    }
    expect(navigationPreviewValidity(frozen, catalog(), { ...context(), frozenPreview: frozen }).valid).toBe(true)
  })

  it('checks frozen integrity before allowing typed refusal display', () => {
    const original = preview()
    const refused: NavigationPreview = { ...original, routes: [], outcomes: original.outcomes.map((item) => ({ ...item, status: 'refused', code: 'unsupported', detail: 'Provider refused this device.' })) }
    const frozen = parseNavigationPreview(refused)!
    expect(navigationPreviewValidity(frozen, catalog(), { ...context(), frozenPreview: frozen }).code).toBe('node_refused')
    const changedRefusal = changed(frozen, ['outcomes', 0, 'detail'], 'A different refusal.')
    expect(navigationPreviewValidity(changedRefusal, catalog(), { ...context(), frozenPreview: frozen }).code).toBe('preview_changed')
  })

  it.each([
    [{ excluded: true }, 'destination_excluded'], [{ reachability: 'unknown' }, 'destination_unreachable'],
    [{ reachability: 'unreachable' }, 'destination_unreachable'], [{ allowedClasses: ['aircraft'] }, 'unsupported_selection'],
  ] as const)('rejects ineligible destination even when dispatchEligible is false: %j', (changes, code) => {
    const accepted = destinationCatalog(changes)
    const report = { ...preview(), destination: accepted.destinations[0] }
    expect(parseNavigationPreview(report)).not.toBeNull()
    expect(navigationPreviewValidity(report, accepted, context()).code).toBe(code)
  })

  it('keeps wrong-floor typed refusal evidence parseable while refusing eligibility', () => {
    const accepted = destinationCatalog({ floorId: 'floor-2' })
    const original = preview()
    const report: NavigationPreview = { ...original, destination: accepted.destinations[0], routes: [], outcomes: original.outcomes.map((item) => ({ ...item, status: 'refused', code: 'wrong_floor' })) }
    expect(parseNavigationPreview(report)).not.toBeNull()
    expect(navigationPreviewValidity(report, accepted, context()).code).toBe('wrong_floor')
  })

  it('requires current catalog/session evidence and does not mistake reported dispatch eligibility for an execution API', () => {
    expect(navigationCatalogValidity(null, 'fixture-session', NOW).code).toBe('catalog_unavailable')
    expect(navigationCatalogValidity(catalog(), 'other-session', NOW).code).toBe('session_changed')
    expect(navigationCatalogValidity(catalog(), 'fixture-session', 999).code).toBe('catalog_not_current')
    expect(navigationCatalogValidity(catalog(), 'fixture-session', Number.NaN).code).toBe('clock_invalid')
    expect(navigationPreviewValidity(null, catalog(), context()).code).toBe('preview_unavailable')
    expect(navigationPreviewValidity(preview(), null, context()).code).toBe('catalog_unavailable')
    expect(parseNavigationPreview({ ...preview(), dispatchEligible: true })).toBeNull()
    expect(NAVIGATION_CONFIRMATION_UNAVAILABLE).toContain('execution unavailable')
  })

  it('accepts a dispatchable preview only with route and artifact evidence', () => {
    const original = preview([selected[0]])
    const qualified: NavigationPreview = {
      ...original,
      dispatchEligible: true,
      execution: {
        planHash: 'd'.repeat(64), mapPin: original.map.mapPin,
        geometryPin: original.map.geometryPin, navigationPin: original.map.navigationPin,
        approvalId: original.map.approvalId, configurationSha256: 'e'.repeat(64),
        permissionZoneIds: ['atrium'],
      },
    }
    const parsed = parseNavigationPreview(qualified)
    expect(parsed).toEqual(qualified)
    expect(navigationPreviewValidity(parsed, catalog(), { ...context(), selected: [selected[0]] }).valid).toBe(true)
    expect(parseNavigationPreview({ ...qualified, execution: { ...qualified.execution!, mapPin: original.map.geometryPin } })).toBeNull()
    const bound = { ...qualified, execution: { ...qualified.execution!, authoringMapPin: original.map.mapPin, mapPin: original.map.geometryPin } }
    expect(parseNavigationPreview(bound)).toEqual(bound)
    expect(parseNavigationPreview({ ...bound, execution: { ...bound.execution, authoringMapPin: original.map.geometryPin } })).toBeNull()
    expect(parseNavigationPreview({ ...bound, execution: { ...bound.execution, authoringMapPin: null } })).toBeNull()
  })
})

describe('unavailable production navigation port', () => {
  it('starts empty, reports why, and exposes neither endpoints nor execution methods', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch')
    const client = new UnavailableNavigationClient()
    const listener = vi.fn()
    const unsubscribe = client.subscribe(listener)
    expect(client.getSnapshot()).toEqual({ status: 'unavailable', reason: NAVIGATION_UNAVAILABLE, catalog: null, preview: null })
    expect(listener).toHaveBeenCalledExactlyOnceWith(client.getSnapshot())
    expect(Object.isFrozen(client.getSnapshot())).toBe(true)
    const accepted = catalog()
    const request: NavigationPreviewRequest = {
      session: accepted.session, intentId: 'intent-1', zoneId: 'atrium', rosterVersion: 7, selected,
      catalogVersion: accepted.catalogVersion, map: accepted.map, configVersion: accepted.configVersion, motionConfig: accepted.motionConfig,
    }
    await expect((client as import('./client').NavigationClient).requestPreview(request)).rejects.toThrow(NAVIGATION_UNAVAILABLE)
    unsubscribe()
    expect(fetcher).not.toHaveBeenCalled()
    expect('execute' in client).toBe(false)
    expect('confirm' in client).toBe(false)
    fetcher.mockRestore()
  })
})
