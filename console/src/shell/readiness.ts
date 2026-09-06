import type { RelayAircraftState } from '../relay/contract'
import { deviceNoun } from '../control/state'
import { humanizeCode } from './format'
import { readinessSentence, ZERO_POSITION_QUALITY_HELP } from './sentences'

/** Display guidance only. Selection and command admission remain relay-owned. */
export function readinessNotes(drone: RelayAircraftState): { code: string | null; text: string }[] {
  const notes: { code: string | null; text: string }[] = drone.readiness_reasons.map((code) => ({
    code,
    text: readinessSentence(code, deviceNoun(drone.device_class)) ?? humanizeCode(code),
  }))
  if (drone.pos_quality === 0) notes.push({ code: null, text: ZERO_POSITION_QUALITY_HELP })
  return notes
}
