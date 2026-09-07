import type { DeviceClass } from '../relay/contract'

/** Shared catalog/preview DTOs mirrored by relay/navigation_service.py. */
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

/** Loaded authoritative planner/safety configuration, opaque to the renderer. */
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
  /** Bind an asynchronous provider result to the request which created it. */
  readonly intentId?: string
  /** Captured parser output when a preview is staged; never replace it with a refresh. */
  readonly frozenPreview?: NavigationPreview
  /** Allows displaying a non-dispatchable refusal before reachability is qualified. */
  readonly reviewOnly?: boolean
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
  /** Read-only service capability; never widens the motion intent profile. */
  readonly reviewSupported?: boolean
}

export interface NavigationValidity {
  readonly valid: boolean
  readonly code: string
  readonly reason: string
}

/** A server check of frozen evidence. This contract never reports motion success. */
export interface NavigationConfirmationOutcome {
  readonly previewId: string
  readonly intentId: string
  readonly status: 'refused' | 'invalidated'
  readonly code: string
  readonly detail: string
  readonly dispatchEligible: false
}

export interface NavigationVerification {
  readonly status: 'idle' | 'verifying' | 'complete' | 'error' | 'invalidated'
  readonly reason: string | null
  readonly outcome: NavigationConfirmationOutcome | null
}

export type NavigationDestinationResolution =
  | { readonly kind: 'resolved'; readonly destination: NavigationDestination }
  | { readonly kind: 'ambiguous'; readonly candidates: readonly NavigationDestination[] }
  | { readonly kind: 'refused'; readonly code: string; readonly reason: string }
