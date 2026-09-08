import type { FormationName } from '../relay/contract'
import type { ControlState } from './state'

/**
 * Existing source-qualified shape contracts. Intent-name advertisement alone is
 * insufficient: NavigationRuntime adds only line via the _mapped_line suffix.
 * PlanningConfig may withdraw altitude, and ground composition preserves shapes.
 * A future profile must publish a shape contract before this console enables it.
 */
export function formationShapeBlockedReason(
  state: Pick<ControlState, 'capabilityProfile'>,
  name?: FormationName,
): string | null {
  const profile = state.capabilityProfile
  if (profile !== null && /^c2_fleet_operations(?:\.no_altitude)?(?:\.ground)?$/.test(profile)) return null
  if (profile !== null && /^[A-Za-z0-9][A-Za-z0-9_.-]*_mapped_line(?:\.ground)?$/.test(profile)) {
    return name === 'line' ? null : 'This relay profile supports the mapped line only; other shapes and formation cycling are unavailable.'
  }
  return `Formation shapes are unreported for relay profile ${profile ?? 'unreported'}; no shape is inferred from formation_set alone.`
}
