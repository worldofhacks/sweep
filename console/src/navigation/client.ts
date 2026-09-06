import type { IntentV1 } from '../relay/contract'

export interface NavigationZone {
  zone_id: string
  floor_id: string
  navigation_allowed: boolean
  arrival_slots: string[]
  aliases: string[]
}

export interface NavigationCatalog {
  floor_id: string
  catalog_version: string
  configuration_id: string
  zones: NavigationZone[]
}

export interface RoutePoint { x_m: number; y_m: number; z_m: number }
export interface AircraftRoute {
  drone: { drone_id: number }
  arrival_slot: { slot_id: string }
  waypoints: RoutePoint[]
}

export interface NavigationPreview {
  session: string
  intent_id: string
  t: number
  expires_at_ms: number
  plan: {
    roster_version: number
    selection: number[]
    navigation: { route: {
      destination_zone_id: string
      execution_order: number[]
      routes: AircraftRoute[]
    } }
  }
}

export interface NavigationClient {
  catalog(sessionId: string): Promise<NavigationCatalog>
  preview(intent: IntentV1): Promise<NavigationPreview>
}

export class HttpNavigationClient implements NavigationClient {
  private readonly config: { baseUrl: string; token: string }
  private readonly fetcher: typeof fetch

  constructor(config: { baseUrl: string; token: string }, fetcher: typeof fetch = fetch) {
    this.config = config
    this.fetcher = fetcher
  }

  async catalog(sessionId: string): Promise<NavigationCatalog> {
    const response = await this.request(sessionId, 'catalog')
    const value = record(response) && response.session === sessionId ? response.catalog : null
    if (!record(value) || typeof value.floor_id !== 'string' ||
      typeof value.catalog_version !== 'string' || typeof value.configuration_id !== 'string' ||
      !Array.isArray(value.zones) || !value.zones.every(zone)) {
      throw new Error('The relay returned an invalid destination catalog.')
    }
    return value as unknown as NavigationCatalog
  }

  async preview(intent: IntentV1): Promise<NavigationPreview> {
    const value = await this.request(intent.session, 'preview', { intent: { ...intent, confirm: true } })
    if (!validPreview(value, intent)) throw new Error('The relay returned an invalid route preview.')
    return value
  }

  private async request(session: string, action: string, body?: object): Promise<unknown> {
    const url = new URL(this.config.baseUrl)
    url.protocol = url.protocol === 'wss:' || url.protocol === 'https:' ? 'https:' : 'http:'
    url.pathname = `/session/${encodeURIComponent(session)}/navigation/${action}`
    url.search = ''
    url.hash = ''
    const fetcher = this.fetcher
    const response = await fetcher(url.toString(), {
      method: body ? 'POST' : 'GET',
      headers: { Authorization: `Bearer ${this.config.token}`, 'Content-Type': 'application/json' },
      ...(body ? { body: JSON.stringify(body) } : {}),
    })
    const value: unknown = await response.json()
    if (!response.ok) {
      throw new Error(record(value) && typeof value.detail === 'string' ? value.detail : 'Navigation is unavailable.')
    }
    return value
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function zone(value: unknown): value is NavigationZone {
  return record(value) && typeof value.zone_id === 'string' && typeof value.floor_id === 'string' &&
    typeof value.navigation_allowed === 'boolean' && Array.isArray(value.arrival_slots) &&
    value.arrival_slots.every(item => typeof item === 'string') && Array.isArray(value.aliases) &&
    value.aliases.every(item => typeof item === 'string')
}

function point(value: unknown): value is RoutePoint {
  return record(value) && ['x_m', 'y_m', 'z_m'].every(key => typeof value[key] === 'number' && Number.isFinite(value[key]))
}

function validPreview(value: unknown, intent: IntentV1): value is NavigationPreview {
  if (!record(value) || value.session !== intent.session || value.intent_id !== intent.intent_id ||
    typeof value.t !== 'number' || !Number.isFinite(value.t) ||
    typeof value.expires_at_ms !== 'number' || !Number.isFinite(value.expires_at_ms) ||
    value.expires_at_ms <= value.t || !record(value.plan)) return false
  const plan = value.plan
  if (!Number.isInteger(plan.roster_version) || !Array.isArray(plan.selection) ||
    JSON.stringify(plan.selection) !== JSON.stringify(intent.selection) || !record(plan.navigation) ||
    !record(plan.navigation.route)) return false
  const route = plan.navigation.route
  return 'zone_id' in intent.args && route.destination_zone_id === intent.args.zone_id &&
    Array.isArray(route.execution_order) && Array.isArray(route.routes) &&
    route.routes.length === intent.selection.length &&
    new Set(route.execution_order).size === intent.selection.length &&
    route.execution_order.every(id => intent.selection.includes(id)) &&
    route.routes.every((item, index) => record(item) && record(item.drone) &&
      item.drone.drone_id === (route.execution_order as unknown[])[index] &&
      record(item.arrival_slot) && typeof item.arrival_slot.slot_id === 'string' &&
      Array.isArray(item.waypoints) && item.waypoints.length >= 2 && item.waypoints.every(point))
}
