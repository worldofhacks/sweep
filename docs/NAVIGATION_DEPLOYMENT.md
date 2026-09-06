# Navigation deployment configuration

Set `SWEEP_NAVIGATION_CONFIG` to a JSON file before enabling navigation. The file pins an accepted map bundle, geometry directory, accepted version hashes, named zones with aliases and arrival slots, permissions, motion limits, and the control-localization store identity. The loader rejects unknown fields, negative limits, malformed pins, and changed configuration.

The artifact callable reloads map and geometry inputs for every preparation and revalidation. Changed bundle or geometry content changes the pin and invalidates an existing preview.

Synthetic deployments support software tests. A remote deployment also needs evidence pinned to the exact map, geometry, motion configuration, speed, and fleet limit. It records localization p95 and maximum-gap limits, clearance and stopping allowances, bridge/deadman/RC probe artifact references, and owner attestation for facts that cannot be checked from a file. The remote profile requires one-aircraft evidence before higher fleet limits are configured.

## Phone route authorization

Remote mapped navigation requires `SWEEP_ENABLE_LOCALIZED_NAVIGATION=true`. The relay then sends a signed route authorization before every mapped `goto`, bound to the plan intent, command, aircraft epoch, map and camera pins, target, start, limits, and segment expiry. A normal `goto` has no route authorization fields and keeps its existing behavior.

The phone receives signed navigation poses only for an active authorized route. A ready pose includes the capture-derived position and a conservative 3D 95% uncertainty radius. The relay assumes the fuser's largest covariance standard deviation is Gaussian and multiplies it by 2.796, the square root of the 3D chi-square 95% critical value 7.815 from NIST's [chi-square critical-value table](https://www.itl.nist.gov/div898/handbook/eda/section3/eda3674.htm). A pose that fails provenance, freshness, tube, or uncertainty checks reports `hold` or `land` with every observation field set to `null`.
