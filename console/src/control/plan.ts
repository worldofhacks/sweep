import type { IntentV1, SearchArgs } from '../relay/contract'
import { formatDroneId, type DeviceLabeller, type PlanPreview } from './state'

const PLAN_TITLES: Partial<Record<IntentV1['name'], string>> = {
  arm: 'Arm session',
  robot_peripheral: 'Robot peripheral',
  camera_control: 'Camera control',
  capture_room: 'Capture room',
  takeoff: 'Takeoff',
  land: 'Land',
  land_all: 'Land all fleet',
  sweep: 'Sweep area',
  navigate: 'Review destination',
  search: 'Search for an object',
}

/** Plan-card title from the design; other intents show their name. */
export function planTitle(intent: IntentV1): string {
  if (intent.name === 'body_pulse' && 'forward_mm_s' in intent.args) {
    return `${intent.args.forward_mm_s > 0 ? 'Forward' : 'Backward'} ${intent.args.duration_ms / 1000} seconds`
  }
  return PLAN_TITLES[intent.name] ?? intent.name
}

/** Ordered plain-language steps from the design's planSteps; `label` names each target by its class. */
export function planSteps(intent: IntentV1, label: DeviceLabeller = formatDroneId): string[] {
  if (intent.name === 'navigate') return [] // Only the authoritative preview may describe routes.
  const ids = intent.selection.map(label).join(', ')
  if (intent.name === 'search') {
    const args = intent.args as SearchArgs
    return [
    `Search ${args.zone_id} for ${args.target_class} using ${ids}.`,
    'The relay must freeze the route and coverage tasks before this mission can be confirmed.',
    'Review findings as observations; acknowledging a finding does not command an aircraft.',
    ]
  }
  if (intent.name === 'camera_control' && 'kind' in intent.args) return [
    `Send ${intent.args.kind === 'photo' ? 'single photo capture' : intent.args.kind === 'ready' ? 'photo-mode preparation' : `absolute gimbal pitch ${'pitch_mdeg' in intent.args ? intent.args.pitch_mdeg / 1000 : ''}°`} only to ${ids}.`,
    'Confirm against the current aircraft connection and SDK capability report.',
    'Completion requires SDK evidence. Photos stay on the aircraft; media download is unavailable.',
  ]
  if (intent.name === 'robot_peripheral' && 'kind' in intent.args) return [`Send ${intent.args.kind} only to ${ids}, independently of the fleet motion selection.`, `Arguments: ${JSON.stringify(intent.args)}`, 'This does not arm or re-enable the drive. Vendor output is reported as submitted; physical completion is not verified.']
  if (intent.name === 'arm') return ['Enable commands for this session. This does not start any aircraft motors.', 'Takeoff is a separate selected-aircraft command and requires another confirmation.']
  if (intent.name === 'body_pulse' && 'forward_mm_s' in intent.args) {
    return [
      `Send only to ${ids}, using each aircraft’s body frame.`,
      `Move ${intent.args.forward_mm_s > 0 ? 'forward' : 'backward'} at ${Math.abs(intent.args.forward_mm_s)} mm/s for ${intent.args.duration_ms} ms.`,
      'The aircraft adapter ends the pulse locally and commands zero velocity. The duration is not a distance guarantee.',
    ]
  }
  if (intent.name === 'ground_velocity' && 'linear_mm_s' in intent.args) return [
    `Send only to ${ids} on its current authenticated connection.`,
    `Request forward ${intent.args.linear_mm_s} mm/s, yaw ${intent.args.angular_mrad_s} mrad/s for ${intent.args.duration_ms} ms.`,
    'The runtime ends this pulse locally. These requested parameters do not guarantee a measured distance or angle.',
    'The relay must still approve the configured clearance, pose, operator and local stop checks.',
  ]
  if (intent.name === 'come_home') return [`Request the configured return for ${ids}.`, 'The relay must resolve an approved return route; the console does not supply or invent one.']
  if (intent.name === 'capture_room' && 'pattern' in intent.args) {
    const args = intent.args
    return [
      `Hold ${ids} at its current pose and confirm the motion gate is clear.`,
      `Capture ${args.pattern} in room ${args.room_id} as capture ${args.capture_id}.`,
      args.pattern === 'pano_360'
        ? 'Produce one full_equirectangular set.'
        : args.pattern === 'single_still'
          ? 'Take one native still at this viewpoint.'
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
  label: DeviceLabeller = formatDroneId,
): PlanPreview {
  const preview: PlanPreview = {
    title: planTitle(intent),
    steps: planSteps(intent, label),
    rosterVersion,
    ...(voiceBinding === undefined ? {} : { voiceBinding }),
  }
  return expiresAt === undefined ? preview : { ...preview, expiresAt }
}
