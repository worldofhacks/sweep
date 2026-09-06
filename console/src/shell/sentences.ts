/**
 * Operator-facing sentences lifted from the Sweep Console v4 design. Codes the
 * relay reports that are absent here fall back to the relay's own detail text.
 */
export const REASONS: Record<string, string> = {
  invalid_payload: 'The request did not match the Intent v1 schema, so the relay never read it.',
  unknown_source: 'The relay does not recognise this connection as an allowed source.',
  unknown_intent: 'The relay has no handler for that intent name.',
  unsupported: 'The relay does not accept this intent at this milestone.',
  duplicate_intent: 'An intent with this id was already accepted; the second copy was dropped.',
  invalid_retry: 'The retry pointed at a request that cannot be retried.',
  session_mismatch: 'The request carried a different session id than this connection.',
  source_mismatch: 'The request claimed a source that does not match the authenticated connection.',
  source_not_allowed: 'This source may not emit this intent.',
  frame_not_allowed: 'The frame type is not permitted on this connection.',
  downstream_error: 'A component behind the relay returned an error.',
  downstream_unavailable: 'A component behind the relay is not reachable.',
  invalid_selection: 'The selection named aircraft the roster does not contain.',
  stale_selection: 'The selection was built against an older roster and no longer holds.',
  stale_roster: 'The roster changed after the plan was built.',
  stale_connection_epoch: 'The aircraft reconnected with a new epoch after the request was built.',
  aircraft_not_registered: "That aircraft is not in this session's roster.",
  aircraft_not_ready: 'The aircraft has open readiness reasons and cannot accept flight commands.',
  invalid_state: "The aircraft's flight state does not allow this command.",
  confirmation_required: 'This intent must be confirmed by the operator before it is sent.',
  armed_required: 'The fleet must be armed before this command is accepted.',
  active_task: 'Another task is already running on this aircraft.',
  estop_active: 'The network stop is active; no motion intent is accepted until the relay reports it clear.',
  geofence: 'The commanded position leaves the geofence.',
  ceiling: 'The commanded altitude is above the indoor ceiling limit.',
  spacing: 'The commanded formation would put two aircraft closer than the spacing limit.',
  battery_reserve: 'Battery is below the reserve needed to finish and come home.',
  battery_critical: 'Battery is critical; only landing is permitted.',
  link_quality: 'Radio link quality is below the limit for commanded motion.',
  link_stale: 'No link report arrived inside the freshness window.',
  position_quality: 'Position quality is below the limit for commanded motion.',
  position_stale: 'No position report arrived inside the freshness window.',
  operator_absent: 'No operator presence was reported at the ground station.',
  control_authority: 'Sweep does not hold control authority for this aircraft.',
  rc_safety_operator_absent: 'The physical RC safety operator is not present beside the aircraft.',
  home_pose_missing: 'No home pose is recorded, so come home and land cannot be planned.',
  invalid_plan: 'The planner produced a plan the arbiter rejected as inconsistent.',
  conflicting_motion: 'Two steps in the plan command the same aircraft to move differently.',
  invalid_roster_transition: 'The roster changed in a way the plan cannot be replayed against.',
  invalid_resume: 'There is no resumable state to continue from.',
  storage: "The aircraft's storage cannot hold the capture set.",
  camera_unsupported: 'The camera does not advertise the requested capture pattern.',
  camera_not_ready: 'The camera reported it is not ready to capture.',
  camera_failure: 'The camera failed during the capture.',
  download_failure: 'The capture files could not be downloaded from the aircraft.',
  adapter_failure: 'The aircraft adapter returned an error.',
  adapter_timeout: 'The aircraft adapter did not answer inside the timeout.',
  planner_failure: 'The planner could not produce a plan for this request.',
}

export const READINESS: Record<string, string> = {
  identity_unverified: "The adapter's identity has not been verified. Check the bridge phone's session and node setup, then reconnect.",
  adapter_capabilities_missing: 'The adapter has not advertised its capabilities. Check that the bridge phone is connected to the aircraft and relay.',
  flight_capability_missing: 'The adapter does not advertise flight control. Check the bridge adapter and aircraft connection before continuing.',
  telemetry_missing: 'No telemetry has arrived. Check the aircraft, RC and bridge phone connections; wait for live telemetry.',
  telemetry_stale: "Telemetry stopped inside the freshness window. Check the bridge phone's LAN and aircraft connections; wait for fresh telemetry.",
  home_pose_missing: 'Home pose is not confirmed. While landed, establish real positioning, then check Readiness → Home pose confirmed on the bridge phone. Fresh positioning must meet the relay’s quality limit.',
  control_authority_missing: 'Sweep control is not granted. Review Readiness → Control authority on the phone with the RC pilot. Resolve connection loss or actual takeover first. Permission does not mean Virtual Stick is enabled.',
  rc_safety_operator_missing: 'Confirm Readiness → RC safety operator present on the bridge phone only with a person at the physical RC; a connection alone is insufficient.',
  disconnected: "Adapter connection lost. Check the bridge phone's LAN and relay connection, then reconnect.",
  leaving: 'The aircraft is completing a graceful leave. Wait for it to finish, then rejoin if needed.',
}

export const ZERO_POSITION_QUALITY_HELP = 'Position quality is 0%. Live telemetry does not establish valid positioning. Check aircraft positioning; the relay’s quality limit still applies.'

export const MEMBERSHIP_REASON: Record<string, string> = {
  authenticated_join: 'The adapter authenticated and joined the roster.',
  authenticated_rejoin: 'The adapter authenticated again with a new connection epoch.',
  readiness_gate_failed: 'A readiness gate failed at join.',
  graceful_leave_requested: 'The aircraft asked to leave.',
  telemetry_recovered: 'Telemetry resumed inside the freshness window.',
  telemetry_stale: 'Telemetry stopped inside the freshness window.',
  graceful_leave_completed: 'The graceful leave finished.',
  adapter_connection_lost: 'The adapter connection dropped without a leave.',
}

export const INVALIDATION: Record<string, string> = {
  stale_roster: 'The roster changed after this plan was built.',
  stale_selection: 'The selection changed after this plan was built.',
  selection_changed: 'The authoritative selection changed after this plan was built.',
  capture_pattern_changed: 'The capture pattern changed after this plan was built.',
  graceful_leave_roster_change: 'An aircraft in the plan completed a graceful leave.',
  aircraft_departed: 'An aircraft in the plan left the session.',
  configuration_changed: 'A configuration change landed after this plan was built.',
  confirmation_window_expired: 'The confirmation window expired before the operator confirmed.',
}

/** The sentence for a request's reason code: refusal and failure codes first, then invalidation codes. */
export function reasonSentence(code: string | undefined): string {
  if (!code) return ''
  return REASONS[code] ?? INVALIDATION[code] ?? ''
}
