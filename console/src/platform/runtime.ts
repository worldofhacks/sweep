import { HttpSurveyCandidateClient } from './survey-client'
import type { ModuleServices } from '../modules/types'
import { UNAVAILABLE_MAP_AUTHORING_CLIENT, type AuthoringOperation } from '../modules/map/authoring/client'
import { UnavailableNavigationClient } from '../navigation'
import { createHttpMapAuthoringClient } from './map-client'
import { HttpNavigationClient } from './navigation-client'
import { isRecord, PlatformHttp, type PlatformConnection, type PlatformFetch } from './http'

const OPERATIONS: readonly string[] = ['list', 'load', 'save', 'validate', 'approve', 'compare', 'observe', 'record', 'activate']

/** Discovery publishes new immutable providers when service capability changes. */
export class PlatformRuntime {
  private services: ModuleServices = { navigation: new UnavailableNavigationClient(), mapAuthoring: UNAVAILABLE_MAP_AUTHORING_CLIENT }
  private listeners = new Set<(services: ModuleServices) => void>()
  private signature = ''
  private generation = 0
  private timer: ReturnType<typeof setTimeout> | undefined
  private request: AbortController | undefined
  private readonly http: PlatformHttp

  constructor(connection: PlatformConnection, fetcher?: PlatformFetch) { this.http = new PlatformHttp(connection, fetcher) }
  getSnapshot() { return this.services }
  subscribe(listener: (services: ModuleServices) => void) {
    this.listeners.add(listener)
    if (this.listeners.size === 1) void this.refresh()
    return () => {
      this.listeners.delete(listener)
      if (!this.listeners.size) { this.generation += 1; this.request?.abort(); clearTimeout(this.timer) }
    }
  }
  private publish(signature: string, services: ModuleServices) {
    if (signature === this.signature) return
    this.signature = signature
    this.services = Object.freeze(services)
    for (const listener of this.listeners) listener(this.services)
  }
  private async refresh() {
    const generation = ++this.generation
    this.request = new AbortController()
    try {
      const value = await this.http.request('/platform', undefined, this.request.signal)
      if (generation !== this.generation) return
      if (!isRecord(value) || value.v !== 1 || value.sessionId !== this.http.connection.sessionId
        || !isRecord(value.mapAuthoring) || !Array.isArray(value.mapAuthoring.operations)
        || value.mapAuthoring.operations.length > OPERATIONS.length
        || value.mapAuthoring.operations.some((operation) => typeof operation !== 'string' || !OPERATIONS.includes(operation))
        || new Set(value.mapAuthoring.operations).size !== value.mapAuthoring.operations.length
        || !isRecord(value.navigation) || typeof value.navigation.review !== 'boolean' || typeof value.navigation.dispatch !== 'boolean') throw new Error('The relay returned an incompatible platform contract.')
      const operations = [...value.mapAuthoring.operations].sort() as AuthoringOperation[]
      const signature = JSON.stringify([value.sessionId, operations, value.navigation.review, value.navigation.dispatch])
      if (signature !== this.signature) this.publish(signature, {
        mapAuthoring: createHttpMapAuthoringClient(this.http, operations),
        surveyCandidates: new HttpSurveyCandidateClient(this.http),
        navigation: value.navigation.review ? new HttpNavigationClient(this.http) : new UnavailableNavigationClient('The relay has disabled destination reviews.'),
      })
    } catch (error) {
      if (generation === this.generation) {
        const reason = error instanceof Error ? error.message : 'Relay platform services are unavailable.'
        this.publish(`unavailable:${reason}`, { navigation: new UnavailableNavigationClient(reason), mapAuthoring: { status: 'unavailable', reason } })
      }
    } finally { if (generation === this.generation && this.listeners.size) this.timer = setTimeout(() => void this.refresh(), 5000) }
  }
}
