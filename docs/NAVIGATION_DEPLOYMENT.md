# Navigation deployment configuration

Set `SWEEP_NAVIGATION_CONFIG` to a JSON file before enabling navigation. The file pins an accepted map bundle, geometry directory, accepted version hashes, named zones with aliases and arrival slots, permissions, motion limits, and a control-store identity. The remote composition requires that identity to match the loaded control-localization configuration. The loader rejects unknown fields, negative limits, malformed pins, and changed configuration.

A zone permits motion only when it appears in `permission_zone_ids` and has `navigation_allowed: true`. Zone IDs are validated even when a zone has no arrival slots. The accepted map bundle supplies authoritative aliases; deployment-file aliases are descriptive.

The artifact callable reloads map and geometry inputs for every preparation and revalidation. Changed bundle or geometry content changes the pin and invalidates an existing preview.

Synthetic deployments support software tests. A remote deployment also needs evidence pinned to the exact map, geometry, motion configuration, speed, and fleet limit. It records localization p95 and maximum-gap limits, clearance and stopping allowances, bridge/deadman/RC probe artifact references, and owner attestation for facts that cannot be checked from a file. The remote profile requires one-aircraft evidence before higher fleet limits are configured.

## Phone route authorization

Remote mapped navigation requires `SWEEP_ENABLE_LOCALIZED_NAVIGATION=true`. The relay then sends a signed route authorization before every mapped `goto`, bound to the plan intent, command, aircraft epoch, map and camera pins, target, start, limits, and segment expiry. A normal `goto` has no route authorization fields and keeps its existing behavior.

The phone receives signed navigation poses only for an active authorized route. A ready pose includes the capture-derived position and its diagnostic 3D 95% uncertainty radius. The relay accepts the pose only within the configured localization and navigation uncertainty bounds. The relay sends subsequent measured poses while the route is active. Missing, stale, expired, or mismatched evidence emits one `hold` with observation fields set to `null` and retires that feed. The phone enforces the authorized route tube and its own freshness bounds.
