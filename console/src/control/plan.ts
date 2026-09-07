import type { IntentV1 } from '../relay/contract'
import { formatDroneId, type PlanPreview } from './state'

const PLAN_TITLES: Partial<Record<IntentV1['name'], string>> = {
  capture_room: 'Capture room',
  takeoff: 'Takeoff',
  land: 'Land',
  land_all: 'Land all fleet',
  sweep: 'Sweep area',
  ground_velocity: 'Move ground node',
  survey_area: 'Record survey area',
}

/** Plan-card title from the design; other intents show their name. */
export function planTitle(intent: IntentV1): string {
  return PLAN_TITLES[intent.name] ?? intent.name
}

/** Ordered plain-language steps from the design's planSteps. */
export function planSteps(intent: IntentV1): string[] {
  const ids = intent.selection.map(formatDroneId).join(', ')
  if (intent.name === 'ground_velocity' && 'linear_mm_s' in intent.args) {
    const args = intent.args
    return [
      args.linear_mm_s > 0
        ? `Drive ${ids} forward at ${args.linear_mm_s} mm/s for ${args.duration_ms} ms.`
        : `Turn ${ids} at ${args.angular_mrad_s} mrad/s for ${args.duration_ms} ms.`,
      'Stop when the pulse ends. Keep the local stop within reach.',
    ]
  }
  if (intent.name === 'survey_area' && 'area_id' in intent.args) {
    return [
      `Record lidar evidence from ${ids} for area ${intent.args.area_id}.`,
      'The operator controls movement during recording.',
      'Complete the recording to save a candidate occupancy map, or cancel to discard it.',
    ]
  }
  if (intent.name === 'capture_room' && 'pattern' in intent.args) {
    const args = intent.args
    return [
      `Hold ${ids} at its current pose and confirm the motion gate is clear.`,
      `Capture ${args.pattern} in room ${args.room_id} as capture ${args.capture_id}.`,
      args.pattern === 'pano_360'
        ? 'Produce one full_equirectangular set.'
        : 'Produce eight overlapping frames with incomplete_vertical_coverage.',
      'Download the file set to the ground station and record checksums and pose metadata.',
    ]
  }
  if (intent.name === 'takeoff') {
    return [
      `Confirm ${ids} is armed and ready.`,
      'Take off to the indoor hover altitude.',
      'Hold and report hovering.',
    ]
  }
  if (intent.name === 'land_all') {
    return [
      'Command every aircraft in the roster to land in place.',
      'Hold the roster until each reports landed.',
      'Leave the fleet armed.',
    ]
  }
  if (intent.name === 'sweep') {
    const area =
      'box' in intent.args
        ? `the exact box x ${intent.args.box.min_x}…${intent.args.box.max_x}, y ${intent.args.box.min_y}…${intent.args.box.max_y}`
        : 'a box derived from the authoritative aircraft positions and spacing'
    return [
      `Assign one deterministic lawnmower lane per aircraft inside ${area}.`,
      'Refuse before dispatch if the requested box or any lane leaves the configured geofence.',
      `Send the frozen lanes to ${ids}.`,
    ]
  }
  return [`Send ${intent.name} to ${ids || 'the roster'}.`]
}

/**
 * The preview a confirmation-gated draft carries into the dock. `expiresAt` is
 * the console-clock deadline a relay-compiled step inherits from its plan: the
 * dock counts it down and the control flow refuses to confirm past it.
 */
export function buildPlanPreview(
  intent: IntentV1,
  rosterVersion: number,
  expiresAt?: number,
  voiceBinding?: PlanPreview['voiceBinding'],
): PlanPreview {
  const preview: PlanPreview = {
    title: planTitle(intent),
    steps: planSteps(intent),
    rosterVersion,
    ...(voiceBinding === undefined ? {} : { voiceBinding }),
  }
  return expiresAt === undefined ? preview : { ...preview, expiresAt }
}
