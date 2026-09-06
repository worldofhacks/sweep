# Navigation deployment configuration

Set `SWEEP_NAVIGATION_CONFIG` to a JSON file before enabling navigation. The file pins an accepted map bundle, geometry directory, accepted version hashes, named zones with aliases and arrival slots, permissions, motion limits, and the control-localization store identity. The loader rejects unknown fields, negative limits, malformed pins, and changed configuration.

A zone permits motion only when it appears in `permission_zone_ids` and has `navigation_allowed: true`. Zone IDs are validated even when a zone has no arrival slots. The accepted map bundle supplies authoritative aliases; deployment-file aliases are descriptive.

The artifact callable reloads map and geometry inputs for every preparation and revalidation. Changed bundle or geometry content changes the pin and invalidates an existing preview.

Synthetic deployments support software tests. A remote deployment also needs evidence pinned to the exact map, geometry, motion configuration, speed, and fleet limit. It records localization p95 and maximum-gap limits, clearance and stopping allowances, bridge/deadman/RC probe artifact references, and owner attestation for facts that cannot be checked from a file. The remote profile requires one-aircraft evidence before higher fleet limits are configured.
