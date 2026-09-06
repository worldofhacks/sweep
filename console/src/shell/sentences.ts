/**
 * Operator-facing sentences lifted from the Sweep Console v4 design. Codes the
 * relay reports that are absent here fall back to the relay's own detail text.
 *
 * A sentence that names the device takes the noun for its class: `aircraft`
 * for aircraft, `robot` for ground vehicles, `device` when the set is mixed
 * or unknown. The exported tables are the aircraft wording; the sentence
 * functions take a noun.
 */
import type { DeviceNoun } from '../control/state'

type Sentence = string | ((noun: DeviceNoun) => string)
type Sentences = Record<string, Sentence>

const article = (noun: DeviceNoun) => (noun === 'aircraft' ? 'an' : 'a')
const plural = (noun: DeviceNoun) => (noun === 'aircraft' ? 'aircraft' : `${noun}s`)
const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

const REASON_SENTENCES: Sentences = {
  invalid_payload: 'The request did not match the Intent v1 schema, so the relay never read it.',
  unknown_source: 'The relay does not recognise this connection as an allowed source.',
  unknown_intent: 'The relay has no handler for that intent name.',
  unsupported: 'The relay does not accept this intent at this milestone.',
  unsupported_for_device_class: (noun) =>
    `This intent is not available for ${plural(noun)}; the relay names the device class in its detail.`,
  duplicate_intent: 'An intent with this id was already accepted; the second copy was dropped.',
  invalid_retry: 'The retry pointed at a request that cannot be retried.',
  session_mismatch: 'The request carried a different session id than this connection.',
  source_mismatch: 'The request claimed a source that does not match the authenticated connection.',
  source_not_allowed: 'This source may not emit this intent.',
  frame_not_allowed: 'The frame type is not permitted on this connection.',
  downstream_error: 'A component behind the relay returned an error.',
  downstream_unavailable: 'A component behind the relay is not reachable.',
  invalid_selection: (noun) => `The selection named ${plural(noun)} the roster does not contain.`,
  stale_selection: 'The selection was built against an older roster and no longer holds.',
  stale_roster: 'The roster changed after the plan was built.',
  stale_connection_epoch: (noun) =>
    `The ${noun} reconnected with a new epoch after the request was built.`,
  aircraft_not_registered: (noun) => `That ${noun} is not in this session's roster.`,
  aircraft_not_ready: (noun) =>
    `The ${noun} has open readiness reasons and cannot accept ${noun === 'aircraft' ? 'flight' : 'motion'} commands.`,
  invalid_state: (noun) =>
    `The ${noun}'s ${noun === 'aircraft' ? 'flight state' : 'drive state'} does not allow this command.`,
  confirmation_required: 'This intent must be confirmed by the operator before it is sent.',
  armed_required: 'The fleet must be armed before this command is accepted.',
  active_task: (noun) => `Another task is already running on this ${noun}.`,
  estop_active: 'The network stop is active; no motion intent is accepted until the relay reports it clear.',
  geofence: 'The commanded position leaves the geofence.',
  ceiling: 'The commanded altitude is above the indoor ceiling limit.',
  spacing: (noun) =>
    `The commanded formation would put two ${plural(noun)} closer than the spacing limit.`,
  battery_reserve: 'Battery is below the reserve needed to finish and come home.',
  battery_critical: (noun) =>
    `Battery is critical; only ${noun === 'robot' ? 'stopping' : 'landing'} is permitted.`,
  link_quality: 'Radio link quality is below the limit for commanded motion.',
  link_stale: 'No link report arrived inside the freshness window.',
  position_quality: 'Position quality is below the limit for commanded motion.',
  position_stale: 'No position report arrived inside the freshness window.',
  operator_absent: 'No operator presence was reported at the ground station.',
  control_authority: (noun) => `Sweep does not hold control authority for this ${noun}.`,
  rc_safety_operator_absent: (noun) =>
    noun === 'robot'
      ? 'The spotter is not present beside the robot with its screen stop in reach.'
      : `The physical RC safety operator is not present beside the ${noun}.`,
  home_pose_missing: (noun) =>
    `No home pose is recorded, so come home${noun === 'robot' ? '' : ' and land'} cannot be planned.`,
  invalid_plan: 'The planner produced a plan the arbiter rejected as inconsistent.',
  conflicting_motion: (noun) => `Two steps in the plan command the same ${noun} to move differently.`,
  invalid_roster_transition: 'The roster changed in a way the plan cannot be replayed against.',
  invalid_resume: 'There is no resumable state to continue from.',
  storage: (noun) => `The ${noun}'s storage cannot hold the capture set.`,
  camera_unsupported: 'The camera does not advertise the requested capture pattern.',
  camera_not_ready: 'The camera reported it is not ready to capture.',
  camera_failure: 'The camera failed during the capture.',
  download_failure: (noun) => `The capture files could not be downloaded from the ${noun}.`,
  adapter_failure: (noun) => `The ${noun} adapter returned an error.`,
  adapter_timeout: (noun) => `The ${noun} adapter did not answer inside the timeout.`,
  planner_failure: 'The planner could not produce a plan for this request.',
}

const READINESS_SENTENCES: Sentences = {
  identity_unverified: "The adapter's identity has not been verified for this session.",
  adapter_capabilities_missing: 'The adapter has not advertised its capabilities.',
  flight_capability_missing: 'The adapter does not advertise flight control.',
  drive_capability_missing: 'The adapter does not advertise ground drive control.',
  telemetry_missing: (noun) => `No telemetry has arrived for this ${noun}.`,
  telemetry_stale: 'Telemetry stopped inside the freshness window.',
  home_pose_missing: (noun) => `No home pose is recorded for this ${noun}.`,
  control_authority_missing: (noun) =>
    noun === 'robot'
      ? 'Sweep does not hold control authority: the wheels are disabled or a local override is active.'
      : 'Sweep does not hold control authority.',
  rc_safety_operator_missing: (noun) =>
    noun === 'robot'
      ? 'No spotter is reported present beside the robot with its screen stop in reach.'
      : 'No RC safety operator is reported present.',
  disconnected: 'The adapter connection is down.',
  leaving: (noun) => `The ${noun} is completing a graceful leave.`,
}

const MEMBERSHIP_REASON_SENTENCES: Sentences = {
  authenticated_join: 'The adapter authenticated and joined the roster.',
  authenticated_rejoin: 'The adapter authenticated again with a new connection epoch.',
  readiness_gate_failed: 'A readiness gate failed at join.',
  device_class_mismatch:
    'The join declared a device class that does not match the relay configuration for this id.',
  graceful_leave_requested: (noun) => `The ${noun} asked to leave.`,
  telemetry_recovered: 'Telemetry resumed inside the freshness window.',
  telemetry_stale: 'Telemetry stopped inside the freshness window.',
  graceful_leave_completed: 'The graceful leave finished.',
  adapter_connection_lost: 'The adapter connection dropped without a leave.',
}

const INVALIDATION_SENTENCES: Sentences = {
  stale_roster: 'The roster changed after this plan was built.',
  stale_selection: 'The selection changed after this plan was built.',
  selection_changed: 'The authoritative selection changed after this plan was built.',
  capture_pattern_changed: 'The capture pattern changed after this plan was built.',
  graceful_leave_roster_change: (noun) =>
    `${capital(article(noun))} ${noun} in the plan completed a graceful leave.`,
  aircraft_departed: (noun) => `${capital(article(noun))} ${noun} in the plan left the session.`,
  configuration_changed: 'A configuration change landed after this plan was built.',
  confirmation_window_expired: 'The confirmation window expired before the operator confirmed.',
}

function render(sentence: Sentence | undefined, noun: DeviceNoun): string | undefined {
  if (sentence === undefined) return undefined
  return typeof sentence === 'string' ? sentence : sentence(noun)
}

function table(sentences: Sentences, noun: DeviceNoun): Record<string, string> {
  return Object.fromEntries(
    Object.entries(sentences).map(([code, sentence]) => [code, render(sentence, noun) as string]),
  )
}

/** The aircraft wording of every table, for the states gallery and callers without a device. */
export const REASONS: Record<string, string> = table(REASON_SENTENCES, 'aircraft')
export const READINESS: Record<string, string> = table(READINESS_SENTENCES, 'aircraft')
export const MEMBERSHIP_REASON: Record<string, string> = table(MEMBERSHIP_REASON_SENTENCES, 'aircraft')
export const INVALIDATION: Record<string, string> = table(INVALIDATION_SENTENCES, 'aircraft')

/** The sentence for a request's reason code: refusal and failure codes first, then invalidation codes. */
export function reasonSentence(code: string | undefined, noun: DeviceNoun = 'aircraft'): string {
  if (!code) return ''
  return render(REASON_SENTENCES[code], noun) ?? render(INVALIDATION_SENTENCES[code], noun) ?? ''
}

export function readinessSentence(code: string, noun: DeviceNoun): string | undefined {
  return render(READINESS_SENTENCES[code], noun)
}

export function membershipReasonSentence(code: string, noun: DeviceNoun): string | undefined {
  return render(MEMBERSHIP_REASON_SENTENCES[code], noun)
}
