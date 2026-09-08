import type { DeviceClass } from '../relay/contract'
import type {
  NavigationCatalog, NavigationContext, NavigationDestination, NavigationDestinationResolution,
  NavigationPreview, NavigationTarget, NavigationValidity,
  NavigationConfirmationOutcome, NavigationExecutionEvidence,
} from './types'

export const MAX_NAVIGATION_TARGETS = 64
export const MAX_NAVIGATION_DESTINATIONS = 128
export const MAX_NAVIGATION_ALIASES = 16
export const MAX_NAVIGATION_WAYPOINTS = 512
export const MAX_NAVIGATION_TOTAL_WAYPOINTS = 8192
export const MAX_NAVIGATION_JSON_BYTES = 1024 * 1024
export const MAX_NAVIGATION_CONFIG_BYTES = 16 * 1024
/** A frontend rendering bound, not a qualified physical motion limit. */
export const MAX_NAVIGATION_COORDINATE_M = 1_000_000

const encoder = new TextEncoder()
const classes: readonly DeviceClass[] = ['aircraft', 'ground_vehicle']
const fail = (code: string, reason: string): NavigationValidity => ({ valid: false, code, reason })
const valid: NavigationValidity = Object.freeze({ valid: true, code: 'valid', reason: 'Frozen preview inputs match current evidence.' })

function record(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function exact(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  return record(value) && Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
}

function text(value: unknown, max = 128): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max &&
    value === value.trim() && !Array.from(value).some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)
}

function identity(value: unknown): value is string {
  return text(value) && /^[A-Za-z0-9][A-Za-z0-9_.:/-]*$/.test(value)
}

function integer(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
}

export function parseNavigationConfirmation(raw: unknown): NavigationConfirmationOutcome | null {
  if (!boundedJson(raw, 16 * 1024) ||
    !exact(raw, ['previewId', 'intentId', 'status', 'code', 'detail', 'dispatchEligible']) ||
    !identity(raw.previewId) || !identity(raw.intentId) || !identity(raw.code) || !text(raw.detail, 2048) ||
    (raw.status !== 'accepted' && raw.status !== 'refused' && raw.status !== 'invalidated') ||
    typeof raw.dispatchEligible !== 'boolean' ||
    ((raw.status === 'accepted') !== (raw.dispatchEligible === true))) return null
  return Object.freeze({ previewId: raw.previewId, intentId: raw.intentId, status: raw.status,
    code: raw.code, detail: raw.detail, dispatchEligible: raw.dispatchEligible })
}

function list(value: unknown, maximum: number, predicate: (item: unknown) => boolean, minimum = 0): value is unknown[] {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum &&
    Array.from(value).every(predicate)
}

/** Reject cycles, accessors, non-JSON objects and oversized input before shape traversal. */
function boundedJson(value: unknown, maxBytes: number, maxDepth = 16, maxItems = 100_000): boolean {
  let remaining = maxItems
  const seen = new Set<object>()
  function walk(item: unknown, depth: number): boolean {
    if (--remaining < 0 || depth > maxDepth) return false
    if (item === null || typeof item === 'boolean') return true
    if (typeof item === 'number') return Number.isFinite(item)
    if (typeof item === 'string') return item.length <= maxBytes
    if (!Array.isArray(item) && !record(item)) return false
    if (seen.has(item)) return false
    seen.add(item)
    const descriptors = Object.getOwnPropertyDescriptors(item)
    if (Object.getOwnPropertySymbols(item).length !== 0) return false
    if (Object.values(descriptors).some((field) => field.get !== undefined || field.set !== undefined)) return false
    if (Object.entries(descriptors).some(([key, field]) => !field.enumerable && !(Array.isArray(item) && key === 'length'))) return false
    const entries = Object.entries(item)
    if (Array.isArray(item) && (entries.length !== item.length || entries.some(([key], index) => key !== String(index)))) return false
    if (entries.some(([key, child]) => key.length > 128 || !walk(child, depth + 1))) return false
    seen.delete(item)
    return true
  }
  return walk(value, 0) && encoder.encode(JSON.stringify(value)).byteLength <= maxBytes
}

function pin(value: unknown): boolean {
  return exact(value, ['version', 'contentSha256']) && identity(value.version) &&
    typeof value.contentSha256 === 'string' && /^[a-f0-9]{64}$/.test(value.contentSha256)
}

function sortedIdentities(value: unknown): boolean {
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_NAVIGATION_DESTINATIONS) return false
  let previous = ''
  for (const item of value) {
    if (!identity(item) || (previous !== '' && previous >= item)) return false
    previous = item
  }
  return true
}

export function isNavigationExecution(value: unknown): value is NavigationExecutionEvidence {
  return exact(value, ['planHash', 'mapPin', 'geometryPin', 'navigationPin', 'approvalId', 'configurationSha256', 'permissionZoneIds',
    ...(record(value) && 'authoringMapPin' in value ? ['authoringMapPin'] : [])]) &&
    (value.authoringMapPin === undefined || pin(value.authoringMapPin)) &&
    typeof value.planHash === 'string' && /^[a-f0-9]{64}$/.test(value.planHash) &&
    pin(value.mapPin) && pin(value.geometryPin) && pin(value.navigationPin) && identity(value.approvalId) &&
    typeof value.configurationSha256 === 'string' && /^[a-f0-9]{64}$/.test(value.configurationSha256) &&
    sortedIdentities(value.permissionZoneIds)
}

function map(value: unknown): value is Record<string, unknown> {
  return exact(value, ['mapId', 'floorId', 'frame', 'mapPin', 'geometryPin', 'navigationPin', 'accepted', 'approvalId']) &&
    identity(value.mapId) && identity(value.floorId) && value.frame === 'world' &&
    value.accepted === true && identity(value.approvalId) &&
    pin(value.mapPin) && pin(value.geometryPin) && pin(value.navigationPin)
}

function target(value: unknown): value is NavigationTarget {
  return exact(value, ['id', 'deviceClass', 'epoch']) && integer(value.id, 1, 2 ** 31 - 1) &&
    classes.includes(value.deviceClass as DeviceClass) && integer(value.epoch, 1)
}

function targets(value: unknown): value is NavigationTarget[] {
  return list(value, MAX_NAVIGATION_TARGETS, target, 1) &&
    new Set(value.map((item) => (item as NavigationTarget).id)).size === value.length
}

function destination(value: unknown): value is NavigationDestination {
  return exact(value, ['zoneId', 'name', 'aliases', 'floorId', 'excluded', 'reachability', 'allowedClasses']) &&
    identity(value.zoneId) && text(value.name) && identity(value.floorId) &&
    list(value.aliases, MAX_NAVIGATION_ALIASES, (item) => text(item)) &&
    new Set(value.aliases.map((alias) => normalized(alias as string))).size === value.aliases.length &&
    typeof value.excluded === 'boolean' && typeof value.reachability === 'string' && ['reachable', 'unreachable', 'unknown'].includes(value.reachability) &&
    list(value.allowedClasses, classes.length, (item) => classes.includes(item as DeviceClass)) &&
    new Set(value.allowedClasses).size === value.allowedClasses.length
}

function motion(value: unknown): boolean {
  return record(value) && Object.keys(value).length > 0 && Object.keys(value).length <= 64 &&
    boundedJson(value, MAX_NAVIGATION_CONFIG_BYTES, 6, 2048)
}

function window(value: Record<string, unknown>): boolean {
  return integer(value.receivedAt) && integer(value.expiresAt) && value.expiresAt > value.receivedAt
}

function point(value: unknown): value is Record<string, unknown> {
  return exact(value, ['xM', 'yM', 'zM', 'floorId', 'frame']) && value.frame === 'world' &&
    identity(value.floorId) && ['xM', 'yM', 'zM'].every((key) =>
      typeof value[key] === 'number' && Number.isFinite(value[key]) && Math.abs(value[key] as number) <= MAX_NAVIGATION_COORDINATE_M)
}

export function isNavigationRoute(value: unknown): value is Record<string, unknown> {
  return exact(value, ['target', 'waypoints', 'arrivalSlot', 'holdBehavior']) && target(value.target) &&
    list(value.waypoints, MAX_NAVIGATION_WAYPOINTS, point, 2) &&
    exact(value.arrivalSlot, ['slotId', 'zoneId', 'position']) && identity(value.arrivalSlot.slotId) &&
    identity(value.arrivalSlot.zoneId) && point(value.arrivalSlot.position) &&
    same(value.waypoints.at(-1), value.arrivalSlot.position) &&
    value.holdBehavior === (value.target.deviceClass === 'aircraft' ? 'hover' : 'stop')
}

function outcome(value: unknown): value is Record<string, unknown> {
  return exact(value, ['target', 'status', 'code', 'detail']) && target(value.target) &&
    typeof value.status === 'string' && ['planned', 'refused'].includes(value.status) && identity(value.code) && text(value.detail, 512)
}

function copyFrozen<T>(value: T): T {
  const copy = JSON.parse(JSON.stringify(value)) as T
  function freeze(item: unknown): void {
    if (item !== null && typeof item === 'object') {
      Object.values(item).forEach(freeze)
      Object.freeze(item)
    }
  }
  freeze(copy)
  return copy
}

export function parseNavigationCatalog(raw: unknown): NavigationCatalog | null {
  try {
    if (!boundedJson(raw, MAX_NAVIGATION_JSON_BYTES) ||
      !exact(raw, ['session', 'catalogVersion', 'receivedAt', 'expiresAt', 'map', 'configVersion', 'motionConfig', 'destinations']) ||
      !text(raw.session, 512) || !identity(raw.catalogVersion) || !window(raw) || !map(raw.map) ||
      !identity(raw.configVersion) || !motion(raw.motionConfig) ||
      !list(raw.destinations, MAX_NAVIGATION_DESTINATIONS, destination) ||
      new Set(raw.destinations.map((item) => (item as NavigationDestination).zoneId)).size !== raw.destinations.length) return null
    return copyFrozen(raw as unknown as NavigationCatalog)
  } catch {
    return null
  }
}

export function parseNavigationPreview(raw: unknown): NavigationPreview | null {
  try {
    if (!boundedJson(raw, MAX_NAVIGATION_JSON_BYTES) ||
      !(exact(raw, ['previewId', 'session', 'intentId', 'rosterVersion', 'selected', 'destination', 'map', 'catalogVersion', 'configVersion', 'motionConfig', 'routes', 'outcomes', 'receivedAt', 'expiresAt', 'dispatchEligible']) ||
        exact(raw, ['previewId', 'session', 'intentId', 'rosterVersion', 'selected', 'destination', 'map', 'catalogVersion', 'configVersion', 'motionConfig', 'routes', 'outcomes', 'receivedAt', 'expiresAt', 'execution', 'dispatchEligible'])) ||
      !identity(raw.previewId) || !text(raw.session, 512) || !identity(raw.intentId) ||
      !integer(raw.rosterVersion) || !targets(raw.selected) || !destination(raw.destination) || !map(raw.map) ||
      !identity(raw.catalogVersion) || !identity(raw.configVersion) || !motion(raw.motionConfig) ||
      !list(raw.routes, MAX_NAVIGATION_TARGETS, isNavigationRoute) || !list(raw.outcomes, MAX_NAVIGATION_TARGETS, outcome, 1) ||
      !window(raw) || typeof raw.dispatchEligible !== 'boolean' ||
      (raw.dispatchEligible ? !isNavigationExecution(raw.execution) : Object.hasOwn(raw, 'execution'))) return null
    const preview = raw as unknown as NavigationPreview
    const selected = new Map(preview.selected.map((item) => [item.id, item]))
    if (preview.outcomes.length !== selected.size || new Set(preview.outcomes.map((item) => item.target.id)).size !== selected.size ||
      preview.outcomes.some((item) => !same(selected.get(item.target.id), item.target)) ||
      new Set(preview.routes.map((item) => item.target.id)).size !== preview.routes.length ||
      new Set(preview.routes.map((item) => item.arrivalSlot.slotId)).size !== preview.routes.length ||
      preview.routes.reduce((total, item) => total + item.waypoints.length, 0) > MAX_NAVIGATION_TOTAL_WAYPOINTS) return null
    const planned = preview.outcomes.filter((item) => item.status === 'planned')
    if (planned.length !== preview.routes.length || preview.routes.some((item) =>
      !same(selected.get(item.target.id), item.target) ||
      !planned.some((entry) => same(entry.target, item.target)) ||
      item.arrivalSlot.zoneId !== preview.destination.zoneId ||
      item.arrivalSlot.position.floorId !== preview.destination.floorId ||
      item.waypoints.some((position) => position.floorId !== preview.map.floorId)) ||
      (preview.dispatchEligible && (planned.length !== selected.size || preview.destination.excluded ||
        preview.destination.reachability !== 'reachable' || preview.selected.some((item) => !preview.destination.allowedClasses.includes(item.deviceClass))))) return null
    if (preview.dispatchEligible && (preview.execution === undefined ||
      !same(preview.execution.authoringMapPin ?? preview.execution.mapPin, preview.map.mapPin) ||
      !preview.execution.permissionZoneIds.includes(preview.destination.zoneId) ||
      preview.selected.length !== 1 || preview.selected[0].deviceClass !== 'aircraft' ||
      preview.routes[0]?.holdBehavior !== 'hover')) return null
    return copyFrozen(preview)
  } catch {
    return null
  }
}

function normalized(value: string): string {
  return value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase('en-US')
}

/** Stable comparison for already bounded JSON, independent of object-key insertion order. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (record(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  return JSON.stringify(value) ?? 'undefined'
}

function same(left: unknown, right: unknown): boolean {
  return canonical(left) === canonical(right)
}

/** Compare parsed JSON evidence without depending on object-key insertion order. */
export const equalNavigationEvidence = same

export function navigationCatalogValidity(catalog: NavigationCatalog | null, session: string, now: number): NavigationValidity {
  if (catalog === null) return fail('catalog_unavailable', 'No accepted destination catalog is available.')
  if (parseNavigationCatalog(catalog) === null) return fail('catalog_invalid', 'The destination catalog does not match the integration contract.')
  if (!integer(now)) return fail('clock_invalid', 'Current time is unavailable.')
  if (catalog.session !== session) return fail('session_changed', 'The catalog belongs to another session.')
  if (now < catalog.receivedAt) return fail('catalog_not_current', 'The catalog observation is in the future.')
  if (now >= catalog.expiresAt) return fail('catalog_expired', 'The accepted destination catalog has expired.')
  return valid
}

export function resolveNavigationDestination(
  catalog: NavigationCatalog | null, query: string, now: number, selectedClasses: readonly DeviceClass[] = [],
  reviewOnly = false,
): NavigationDestinationResolution {
  const current = navigationCatalogValidity(catalog, catalog?.session ?? '', now)
  if (!current.valid || catalog === null) return { kind: 'refused', code: current.code, reason: current.reason }
  if (typeof query !== 'string' || query.length > 512 || !normalized(query)) return { kind: 'refused', code: 'destination_missing', reason: 'Enter a destination name or alias.' }
  const name = normalized(query)
  const matches = catalog.destinations.filter((item) => [item.zoneId, item.name, ...item.aliases].some((candidate) => normalized(candidate) === name))
  if (matches.length === 0) return { kind: 'refused', code: 'destination_unknown', reason: 'No accepted destination matches this name.' }
  if (matches.length > 1) return { kind: 'ambiguous', candidates: matches }
  return destinationEligibility(catalog, matches[0], selectedClasses, reviewOnly)
}

/** Explicit selection is a canonical identity, never reinterpreted as another zone's alias. */
export function resolveNavigationZoneId(
  catalog: NavigationCatalog | null, zoneId: string, now: number, selectedClasses: readonly DeviceClass[] = [],
  reviewOnly = false,
): NavigationDestinationResolution {
  const current = navigationCatalogValidity(catalog, catalog?.session ?? '', now)
  if (!current.valid || catalog === null) return { kind: 'refused', code: current.code, reason: current.reason }
  const found = catalog.destinations.find((item) => item.zoneId === zoneId)
  if (!found) return { kind: 'refused', code: 'destination_unknown', reason: 'This canonical destination is absent from the accepted catalog.' }
  return destinationEligibility(catalog, found, selectedClasses, reviewOnly)
}

function destinationEligibility(
  catalog: NavigationCatalog, found: NavigationDestination, selectedClasses: readonly DeviceClass[],
  reviewOnly: boolean,
): NavigationDestinationResolution {
  if (found.excluded) return { kind: 'refused', code: 'destination_excluded', reason: 'This destination is excluded from navigation.' }
  if (found.floorId !== catalog.map.floorId) return { kind: 'refused', code: 'wrong_floor', reason: 'This destination is on another floor.' }
  if (found.reachability !== 'reachable' && !(reviewOnly && found.reachability === 'unknown')) return { kind: 'refused', code: 'destination_unreachable', reason: found.reachability === 'unknown' ? 'Destination reachability has not been established.' : 'This destination is unreachable.' }
  if (selectedClasses.some((item) => !found.allowedClasses.includes(item))) return { kind: 'refused', code: 'unsupported_selection', reason: 'This destination does not support every selected device class.' }
  return { kind: 'resolved', destination: found }
}

export function navigationPreviewValidity(
  preview: NavigationPreview | null, catalog: NavigationCatalog | null, context: NavigationContext,
): NavigationValidity {
  const current = navigationCatalogValidity(catalog, context.session, context.now)
  if (!current.valid || catalog === null) return current
  if (preview === null) return fail('preview_unavailable', 'No frozen navigation preview is available.')
  if (parseNavigationPreview(preview) === null) return fail('preview_invalid', 'The navigation preview does not match the integration contract.')
  if (preview.session !== context.session) return fail('session_changed', 'The preview belongs to another session.')
  if (context.intentId !== undefined && preview.intentId !== context.intentId) return fail('intent_changed', 'The preview belongs to another request.')
  if (context.now < preview.receivedAt) return fail('preview_not_current', 'The preview observation is in the future.')
  if (context.now >= preview.expiresAt) return fail('preview_expired', 'The navigation preview has expired.')
  if (preview.rosterVersion !== context.rosterVersion) return fail('roster_changed', 'The roster changed after preview.')
  if (!targets(context.selected) || !same(preview.selected, context.selected)) return fail('selection_changed', 'Selected device IDs, classes, epochs or order changed after preview.')
  if (preview.destination.zoneId !== context.destinationZoneId) return fail('destination_changed', 'The requested destination changed after preview.')
  const destination = catalog.destinations.find((item) => item.zoneId === preview.destination.zoneId)
  const qualifiedDestination = preview.dispatchEligible && destination?.reachability === 'unknown'
    ? { ...destination, reachability: 'reachable' as const }
    : destination
  if (!same(preview.destination, qualifiedDestination)) return fail('destination_changed', 'The accepted destination changed after preview.')
  if (preview.catalogVersion !== catalog.catalogVersion) return fail('catalog_changed', 'The destination catalog version changed after preview.')
  if (!same(preview.map, catalog.map)) return fail('map_changed', 'The accepted map, geometry or navigation artifact changed after preview.')
  if (preview.configVersion !== catalog.configVersion || !same(preview.motionConfig, catalog.motionConfig)) return fail('motion_config_changed', 'The authoritative motion configuration changed after preview.')
  if (context.frozenPreview !== undefined && (parseNavigationPreview(context.frozenPreview) === null || !same(preview, context.frozenPreview))) return fail('preview_changed', 'The captured preview, route, arrival slot or hold behavior changed. Request a new preview.')
  if (destination?.excluded) return fail('destination_excluded', 'This destination is excluded from navigation.')
  if (destination?.floorId !== catalog.map.floorId) return fail('wrong_floor', 'This destination is on another floor.')
  if (qualifiedDestination?.reachability !== 'reachable' && !(context.reviewOnly === true && preview.dispatchEligible === false && destination?.reachability === 'unknown')) return fail('destination_unreachable', 'Destination reachability has not been established for this preview.')
  if (preview.selected.some((item) => !destination.allowedClasses.includes(item.deviceClass))) return fail('unsupported_selection', 'This destination does not support every selected device class.')
  if (preview.outcomes.some((item) => item.status === 'refused')) return fail('node_refused', 'At least one selected device was refused by the preview provider.')
  return valid
}
