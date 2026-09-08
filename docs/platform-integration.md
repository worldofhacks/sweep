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
| Qualified aircraft dispatch | Generate and retain one flight-deployment route, then dispatch that exact plan once after confirmation | One selected hovering aircraft, matching authoring-map and navigation-artifact pin, measured localization, signed approval and current arbiter checks |
| Position overlays | Display fresh current-epoch world poses with source/confidence/map associations | Authenticated, host-qualified producer and measured registration |
| Drive-over recording | Record one selected ground robot's current associated position with a durable tag/actor/source audit | Fresh qualified world pose and an explicit operator association; tape verification remains separate |

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

Position and drive-over requests require the exact saved
`reference: {bundleId, revision, contentHash}` as well as `mapVersion` and `floorId`.
The server requires that reference to be the active approved revision and the
producer's host-qualified registration to bind the same revision. Position
responses, each observation and each capture receipt retain that reference;
matching map labels alone cannot associate evidence with another draft.

## Explicit downstream boundaries

#143 freezes the shared identity and review contract. When a signed flight deployment
is loaded, one selected hovering aircraft can receive a clearance-checked route only
when the active authoring map pin equals the route artifact's map pin. The retained
preview carries its plan hash, artifact pins, approval, configuration hash and
permitted zones. Confirmation consumes that preview once, rechecks the frozen inputs,
then sends the retained plan through the existing planner, arbiter and phone route wire;
each segment still revalidates. #249 supplies ground routes and execution. Static map
approval or a device class alone never enables any of those paths. C1/C2 retain their
existing motion capabilities. Previews without the flight deployment report
`dispatchEligible: false`; navigation confirmation returns an execution-unavailable
result. Reviewing a destination grants no takeoff, capture, survey or formation action.

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
