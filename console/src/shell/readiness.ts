import type { RelayAircraftState } from '../relay/contract'
import { deviceNoun } from '../control/state'
import { humanizeCode } from './format'
import {
  readinessSentence,
  SUPERVISED_VERTICAL_ZERO_POSITION_QUALITY_HELP,
  ZERO_POSITION_QUALITY_HELP,
} from './sentences'

/** Display guidance only. Selection and command admission remain relay-owned. */
export function readinessNotes(
  drone: RelayAircraftState,
  capabilityProfile: string | null = null,
): { code: string | null; text: string }[] {
  const notes: { code: string | null; text: string }[] = drone.readiness_reasons.map((code) => ({
    code,
    text: readinessSentence(code, deviceNoun(drone.device_class)) ?? humanizeCode(code),
  }))
  const safety = drone.node_status?.device_telemetry?.safety
  if (safety && typeof safety === 'object' && !Array.isArray(safety) && safety.blocked === true && Array.isArray(safety.reasons)) {
    for (const reason of safety.reasons) {
      if (typeof reason === 'string' && !notes.some((note) => note.code === reason)) {
        notes.push({ code: reason, text: `Robot safety guard reports ${humanizeCode(reason).toLowerCase()}. Motion remains blocked until the guard clears.` })
      }
    }
  }
  if (drone.pos_quality === 0) {
    notes.push({
      code: null,
      text: capabilityProfile === 'supervised_vertical'
        ? SUPERVISED_VERTICAL_ZERO_POSITION_QUALITY_HELP
        : ZERO_POSITION_QUALITY_HELP,
    })
  }
  return notes
}
