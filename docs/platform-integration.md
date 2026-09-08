# Console and relay platform integration

The single operator console discovers the configured relay's authenticated
platform API and supplies real HTTP clients to the existing Navigate and Map
authoring panes. An older relay without those services remains visibly
unavailable. Installing a console build alone does not upgrade the running relay.
Neither the console nor the platform services create runtime devices, map images,
routes, world positions or successful approvals as substitutes for missing data.

## Implemented software

| Surface | Implemented behavior | Required real input |
| --- | --- | --- |
| Map authoring (#248) | Import, edit, validate, save immutable versions, explicitly approve, reload and compare | Saved occupancy image, actual dimensions/resolution/origin, metric world registration, creation evidence, geometry and tag measurements |
| Active map | Explicitly select an exact approved revision with a durable actor/audit receipt | Current approved revision in the authenticated session |
| Navigation review (#143) | Resolve names/aliases, clarify ambiguity, capture selection/class/epoch, freeze map/configuration/routes/outcomes, and revalidate a one-shot confirmation | Current approved map, authoritative relay state and the loaded planner/safety configuration |
| Position overlays | Display fresh current-epoch world poses with source/confidence/map associations | Authenticated, host-qualified producer and measured registration |
| Drive-over recording | Record one selected ground robot's current associated position with a durable tag/actor/source audit | Fresh qualified world pose and an explicit operator association; tape verification remains separate |
| Pilot-assisted ground survey | Confirmed start, exact run/epoch completion or cancel, verified occupancy preview/download | Ready selected ground robot, accepted canonical pose/scans and immutable completed candidate |

The relay mounts these operations under `/api/sessions/{session_id}`. Console
operations use the existing console bearer credential. Observation ingestion
requires a separately authenticated, device-bound adapter or localization
principal. Requests and responses are bounded; state is never supplied by a
browser as execution authority. Discovery advertises only the available services.
See [the navigation wire contract](../relay/navigation_wire.py), [the shared observation
contract](../spatial/README.md), and [the console workflow](../console/README.md).

Map state resides under the configured relay log directory's `platform` folder.
The map store retains immutable draft revisions and publishes validated
`sweep-world-bundle-v1` documents with content hashes, original image bytes,
geometry, tag provenance, explicit registration and approval audit. The legacy
`building`-frame bundle validator remains separate; renaming that frame to
`world` does not establish a registration.

Navigation's static geometry/catalog hashes identify authoring evidence. They do
not claim generated flight-clearance geometry. Published state transitions, map
edits/approvals/selections, expired evidence, changed configuration and process
restarts retire old reviews. A → B → A cannot revive a captured confirmation or
old live map position. Stale or unavailable telemetry never becomes a live pose.

Immutable map storage survives a process restart, but a persisted relay session
cannot become live again. A new live session requires an explicit draft import,
validation and approval; it does not inherit the previous session's map authority.

Position and drive-over requests require the exact saved
`reference: {bundleId, revision, contentHash}` as well as `mapVersion` and `floorId`.
The server requires that reference to be the active approved revision and the
producer's host-qualified registration to bind the same revision. Position
responses, each observation and each capture receipt retain that reference;
matching map labels alone cannot associate evidence with another draft.

Survey completion acknowledgments carry `candidate_id`, `run_id` and
`connection_epoch`. The authenticated
`GET /api/sessions/{session_id}/survey-candidates/{candidate_id}` returns the
recorded occupancy PNG and manifest only after inventory hashes and independent
recording/source/frame/pose provenance agree. Responses use `Cache-Control:
no-store`, metadata is bounded to 64 KiB, image bytes to 16 MiB, and the complete
encoded response to 24 MiB. There is no caller-supplied artifact path. Candidates
stay in their source-scoped local odometry frame with `navigation_authority:
false`; they are inputs to measured map authoring, not flight approvals.

## Explicit downstream boundaries

#143 freezes the shared identity and review contract. #144 consumes it to generate
clearance-checked aircraft routes; #145 executes frozen aircraft plans with
segment-by-segment revalidation. #249 supplies ground routes and execution. Their
qualified route/arbiter integration is not inferred from static map approval or a
device's class. C1/C2 retain their existing motion capabilities. Current platform
previews report `dispatchEligible: false`; navigation confirmation returns an
execution-unavailable result. Reviewing a destination grants no takeoff, capture,
survey or formation action.

#248's software acceptance uses complete isolated bundle fixtures. A real map can
replace those inputs through the same editor flow, but test fixtures are never
loaded by the operator runtime. Actual saved-map evidence remains #247. Camera
calibration, SLAM-to-world transforms, independent measured ties and verified tag
candidates remain #243. The production Ohmni observation/control runtime and its
hardware deadman, local stop, cameras and LiDAR acceptance remain #246. This
integration does not close those issues or qualify motion on powered-off devices.

## Validation and deployment evidence

Before merging, run the console tests, lint, TypeScript and production build, plus
the map validator/store, platform HTTP, shared observation and navigation
schema/router/compiler tests. Check the actual Python catalog/preview output with
the production TypeScript parsers. Acceptance includes immutable revision and
approval identity, reload/reapproval, invalid geometry/registration/tag refusal,
mixed-device outcomes, stale epochs, late replies, expiry, cancellation, storage
failures and map-selection invalidation. Fixtures belong only in those tests.

Deployment additionally requires the real relay configuration to load without
inventing missing physical values. Only the configured console port should be
served, using the same current relay session and credentials. Verify discovery,
saved-map operations and unavailable/offline states against the deployed build.
When devices are powered off, report hardware and motion qualification as pending;
passing software tests does not establish camera, LiDAR, pose-source or route
performance on physical devices.
