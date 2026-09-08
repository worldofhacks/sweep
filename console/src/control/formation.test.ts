import { expect, test } from 'vitest'
import { formationShapeBlockedReason } from './formation'
import { FORMATION_NAMES } from '../relay/contract'

test.each(['c2_fleet_operations', 'c2_fleet_operations.ground', 'c2_fleet_operations.no_altitude', 'c2_fleet_operations.no_altitude.ground'])('source-qualified C2 profile %s preserves its shape contract', (capabilityProfile) => {
  for (const shape of FORMATION_NAMES) expect(formationShapeBlockedReason({ capabilityProfile }, shape)).toBeNull()
  expect(formationShapeBlockedReason({ capabilityProfile })).toBeNull()
})

test.each(['c1_basic_control_mapped_line', 'c1_basic_control.ground_mapped_line', 'c1_basic_control.no_altitude.ground_mapped_line', 'c1_basic_control_mapped_line.ground'])('source-qualified mapped-line profile %s permits line only', (capabilityProfile) => {
  expect(formationShapeBlockedReason({ capabilityProfile }, 'line')).toBeNull()
  for (const shape of FORMATION_NAMES.filter((shape) => shape !== 'line')) expect(formationShapeBlockedReason({ capabilityProfile }, shape)).toContain('mapped line only')
  expect(formationShapeBlockedReason({ capabilityProfile })).toContain('mapped line only')
})

test.each([null, 'c1_basic_control', 'future_profile', 'mapped_line', 'prefix_c2_fleet_operations', 'c2_fleet_operations_extra', 'c1_mapped_line_extra'])('unknown formation profile %s remains unavailable', (capabilityProfile) => {
  for (const shape of FORMATION_NAMES) expect(formationShapeBlockedReason({ capabilityProfile }, shape)).toContain('Formation shapes are unreported')
})
