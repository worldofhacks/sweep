import type { DeviceClass } from '../relay/contract'

/** Console integration model for #143. This is not an implemented backend wire schema. */
export interface ArtifactPin {
  readonly version: string
  readonly contentSha256: string
}

export interface NavigationMapRef {
  readonly mapId: string
  readonly floorId: string
  readonly frame: 'world'
  readonly mapPin: ArtifactPin
  readonly geometryPin: ArtifactPin
  readonly navigationPin: ArtifactPin
  readonly accepted: true
  readonly approvalId: string
}

export interface NavigationTarget {
  readonly id: number
  readonly deviceClass: DeviceClass
  readonly epoch: number
}

export interface NavigationDestination {
  readonly zoneId: string
  readonly name: string
  readonly aliases: readonly string[]
  readonly floorId: string
  readonly excluded: boolean
  readonly reachability: 'reachable' | 'unreachable' | 'unknown'
  readonly allowedClasses: readonly DeviceClass[]
}

export type NavigationJsonValue = null | boolean | number | string |
  readonly NavigationJsonValue[] | { readonly [key: string]: NavigationJsonValue }

/** Opaque measured configuration until the backend publishes its physical schema. */
export type NavigationMotionConfig = { readonly [key: string]: NavigationJsonValue }

export interface NavigationCatalog {
  readonly session: string
  readonly catalogVersion: string
  readonly receivedAt: number
  readonly expiresAt: number
  readonly map: NavigationMapRef
  readonly configVersion: string
  readonly motionConfig: NavigationMotionConfig
  readonly destinations: readonly NavigationDestination[]
}

export interface NavigationPoint {
  readonly xM: number
  readonly yM: number
  readonly zM: number
  readonly floorId: string
  readonly frame: 'world'
}

export interface NavigationRoute {
  readonly target: NavigationTarget
  readonly waypoints: readonly NavigationPoint[]
  readonly arrivalSlot: {
    readonly slotId: string
    readonly zoneId: string
    readonly position: NavigationPoint
  }
  readonly holdBehavior: 'hover' | 'stop'
}

export interface NavigationNodeOutcome {
  readonly target: NavigationTarget
  readonly status: 'planned' | 'refused'
  readonly code: string
  readonly detail: string
}

export interface NavigationPreview {
  readonly previewId: string
  readonly session: string
  readonly intentId: string
  readonly rosterVersion: number
  readonly selected: readonly NavigationTarget[]
  readonly destination: NavigationDestination
  readonly map: NavigationMapRef
  readonly catalogVersion: string
  readonly configVersion: string
  readonly motionConfig: NavigationMotionConfig
  readonly routes: readonly NavigationRoute[]
  readonly outcomes: readonly NavigationNodeOutcome[]
  readonly receivedAt: number
  readonly expiresAt: number
  /** Reported evidence only; this frontend contract never grants execution. */
  readonly dispatchEligible: boolean
}

export interface NavigationContext {
  readonly session: string
  readonly rosterVersion: number
  readonly selected: readonly NavigationTarget[]
  readonly destinationZoneId: string
  readonly now: number
}

export interface NavigationPreviewRequest {
  readonly session: string
  readonly intentId: string
  readonly zoneId: string
  readonly rosterVersion: number
  readonly selected: readonly NavigationTarget[]
  readonly catalogVersion: string
  readonly map: NavigationMapRef
  readonly configVersion: string
  readonly motionConfig: NavigationMotionConfig
}

export interface NavigationSnapshot {
  readonly status: 'unavailable' | 'loading' | 'ready' | 'error'
  readonly reason: string | null
  readonly catalog: NavigationCatalog | null
  readonly preview: NavigationPreview | null
}

export interface NavigationValidity {
  readonly valid: boolean
  readonly code: string
  readonly reason: string
}

export type NavigationDestinationResolution =
  | { readonly kind: 'resolved'; readonly destination: NavigationDestination }
  | { readonly kind: 'ambiguous'; readonly candidates: readonly NavigationDestination[] }
  | { readonly kind: 'refused'; readonly code: string; readonly reason: string }
