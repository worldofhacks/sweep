/**
 * Operator-facing sentences lifted from the Sweep Console v4 design. Codes the
 * relay reports that are absent here fall back to the relay's own detail text.
 */
export const READINESS: Record<string, string> = {
  identity_unverified: "The adapter's identity has not been verified for this session.",
  adapter_capabilities_missing: 'The adapter has not advertised its capabilities.',
  flight_capability_missing: 'The adapter does not advertise flight control.',
  telemetry_missing: 'No telemetry has arrived for this aircraft.',
  telemetry_stale: 'Telemetry stopped inside the freshness window.',
  home_pose_missing: 'No home pose is recorded for this aircraft.',
  control_authority_missing: 'Sweep does not hold control authority.',
  rc_safety_operator_missing: 'No RC safety operator is reported present.',
  disconnected: 'The adapter connection is down.',
  leaving: 'The aircraft is completing a graceful leave.',
}

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
  aircraft_departed: 'An aircraft in the plan left the session.',
  configuration_changed: 'A configuration change landed after this plan was built.',
  confirmation_window_expired: 'The confirmation window expired before the operator confirmed.',
}
