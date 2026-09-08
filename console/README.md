# console

Capability area: Interaction. Milestone: M0 onward.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to shared contracts or safety-critical paths name one change owner and require cross-review.

The operator console: map, gesture readout, ledger, video mosaic, focus pane, attention promotion, health strip, and the language input with plan preview. A static web app; all state comes from the relay over WebSocket.

Stack: Vite, React, TypeScript, pnpm. Webcam hand landmarks come from MediaPipe Tasks.

For the laptop operator console, use `python3 tools/console.py start` from the repository root.
The one URL is **http://127.0.0.1:5173/**. [Operating guide](../docs/laptop-console.md).
The commands below are for console development; the development server uses that same fixed port.

    pnpm install
    pnpm dev        # http://127.0.0.1:5173; refuses occupied/alternate ports
    pnpm lint
    pnpm test       # deterministic contract, reducer, client, and component tests
    pnpm build      # static files in dist/

Every running console uses real relay data. Synthetic scenarios and recorded gesture sessions are isolated test inputs and cannot be selected by a browser URL.

PRD: sections 4.2, 5.8.

## Relay bootstrap

Production has no simulator or fixture fallback. The hosting shell either sets an in-memory runtime
bootstrap before `main.tsx` runs:

```ts
window.__SWEEP_RELAY_CONFIG__ = {
  baseUrl: 'wss://relay.example.internal',
  sessionId: 'active-session-id',
  token: '<relay token supplied by the trusted local shell>',
}
```

or serves the same three values as same-origin JSON at `/relay-bootstrap.json`, shaped
`{ "relay": { "baseUrl", "sessionId", "token" } }`. `main.tsx` prefers the global; without it the
endpoint is read exactly once (`src/relay/bootstrap.ts`) before the runtime is created, and only a
complete payload whose `baseUrl` parses as `ws:` or `wss:` is accepted. `pnpm dev` serves that
endpoint from the relay's own variables, so one exported `.env` serves both processes:
`SWEEP_RELAY_ORIGIN`, `SWEEP_SESSION_ID`, and `SWEEP_RELAY_TOKEN` (all explicit, no defaults).
With configuration missing or invalid it answers 503 with `{ "relay": null }` and
the console runs as before: visibly disconnected, network controls unavailable, no retry. The built
`dist/` contains neither the endpoint nor the token, so a production host must serve the same JSON
at that path or set the global itself. URL query parameters cannot switch to fixtures.

The client opens `/ws/{session_id}` four times: one connection authenticates as `console` for
buttons and state, a separate connection authenticates as `keyboard` for the Shift+Escape network
stop, a third authenticates as `webcam` for the gesture producer, and a fourth authenticates as
`language` for exact relay-compiled plan steps. An intent is never moved
between those sources, and no connection retries silently. A relay that does not register the
`webcam` source refuses that connection; the Gesture module shows the refusal and emits nothing.
The token is sent only in the first WebSocket frame; it is never placed in a URL, rendered in the
UI, or included in console logging. Without the full bootstrap, the sources remain visibly
disconnected and network controls are unavailable.

## Modular inventory

Sweep is additive: the current scope includes at least five ground robots, each with two
onboard cameras and one LiDAR, plus aircraft with one camera and a reported infrared
depth/proximity sensor pending identification and validation. Scope does not create live rows.
Only actual relay-reported devices and explicitly configured onboard cameras appear; an
unconfigured second camera is never duplicated from the primary feed. [Integration guide](../docs/modular-fleet.md).

## Device classes

Every device the relay reports carries `device_class` (`aircraft` or `ground_vehicle`) and `unit`,
its 1-based ordinal within that class; a relay that omits them is read as aircraft whose unit is
the drone id (`src/relay/contract.ts`, `normalizeRelayAircraftState`). Labels are `D-{unit}` for
aircraft and `G-{unit}` for ground vehicles (`formatDeviceId`; `formatDroneId` stays as the
aircraft-only alias), and operator copy takes the noun for the class in view: `aircraft`, `robot`,
or `device` when a set is mixed or empty (`deviceNoun`, `rosterNoun`, `selectionNoun` in
`src/control/state.ts`; the sentence tables in `src/shell/sentences.ts` take the same noun). A
join's `class:<device_class>` capability sets the class before the first state frame arrives.

The Control module marks `takeoff`, `land`, `land_all`, `altitude`, `sweep` and `capture_room`
unsupported with "Not available for robots." when every device a control addresses is a ground
vehicle (`deviceClassBlockedReason`); a mixed selection keeps them enabled because the relay
decides per device, and `land_all` follows the roster, so it is unsupported only when the roster
holds no aircraft. Body pulses require an aircraft-only selection, even when a robot advertises
the pulse capability. The opt-in Flight profile also rejects any selection containing a robot.
For a ground vehicle the registry's safety-operator line reads `Spotter` (a person beside the robot
with its screen stop in reach). Missing authority reads `Sweep control not granted`; that flag
alone does not prove an RC takeover or local override. Readiness help gives class-specific setup
steps and shows zero position quality independently of live telemetry or membership.

A node's `sensor` frame (a `lidar_scan`: pose at scan time, angle origin and increment, range
bounds, and integer centimetre ranges with 0 for no return) is parsed with the relay's bounds
(`RelaySensorEvent`, `parseRelayServerEvent`) and kept in `src/sensor/store.ts`: the latest scan per
device and a trail of the last twenty, read with `useSensorStore(controller.sensors)`. The control
reducer records only `sensor.last_scan_at`, mirroring the relay's projection. `MapMetadata` and
`parseMapMetadata` read the headers of the relay's map endpoint for the fleet map.

## Survey and run evidence

Control → Ground records one confirmed pilot-assisted survey and closes the exact acknowledged run/epoch. A completed candidate can be fetched through the authenticated platform provider, verified and downloaded with its original occupancy PNG and provenance. It remains unregistered local evidence; Map validation/approval requires actual measured registration. See [the lifecycle contract](../docs/survey-area.md).

Control → Requests summarizes retained session request outcomes and separately stores explicitly unverified operator observations. It has no automatic checklist completion or scripted command playback. Request completion does not establish physical movement.

## Camera dashboard

The Live module's single wall and device inspection use the authoritative device ID, class, unit, connection
epoch, telemetry, membership, readiness reasons, and a closed media status with a last-frame
timestamp. The console uses explicitly reported camera IDs, labels, safe configured stream names, and per-camera status. With no camera mapping it retains one legacy primary stream, `drone{unit}` for aircraft or `ground{unit}` for robots. It never renders an arbitrary adapter-provided media URL. `All devices` is the default for every roster: its responsive
wall has one tile per reported device with selection among its explicitly configured onboard cameras, adds new joins automatically, and keeps known offline states visible. An empty configured-camera list remains empty. There are no empty hardware slots or six-device display cap; it supports
the configured bounded mixed-fleet inventory. Focus opens local device inspection with
a Back to All devices action; inspection changes no command selection. All cameras adds one tile per unique, explicitly reported camera stream, allowing two two-camera robots and two one-camera aircraft to occupy six tiles. A duplicate stream mapping is withheld, and an already inspected mapping retires until the operator explicitly chooses a current camera. With device IDs
`1,2,11,12,13` configured as two aircraft and three robots, the paths are `drone1`, `drone2`,
`ground1`, `ground2`, `ground3`; command envelopes retain the global IDs. One console uses one
authoritative relay/session; it does not combine rosters from separate relay instances.

See [the four-device demo workflow](../docs/four-device-demo.md) for six-feed checks and physical acceptance.

## Live playback

Every wall tile whose stream the relay reports `live` plays it over WHEP in its own session,
including mixed aircraft and ground-vehicle walls. The focus feed plays the focused device's
stream the same way; playback needs the page to have been served a media configuration, and every
other state is said in words. A player is torn down with its tile: when the pane changes, when the
console unmounts, and the moment the relay stops reporting the stream `live`, after which the tile
says `offline` with the age of the last frame the relay knew about. The relay's `video` field
(`relay/README.md`, "Membership and state fan-out") is the only source of that status; the console
never probes MediaMTX itself. A changed device connection epoch also replaces that device's
player, even if the console did not observe an intervening offline frame. Other players remain open.

Source availability and browser playback are separate evidence. A tile reports playback only
after a video frame arrives. Failed connections clean up their old peer and WHEP session, then
retry after 1, 2, 4, and at most 8 seconds while the stream remains live. A long initial H264
keyframe interval may require up to the separate 20-second first-frame window; signaling phases
have 5-second bounds. Once playing, a visible tile that receives no fresh rendered frame for
3 seconds reports the stall and reconnects. Background tabs wait for fresh video when brought
forward. An offline transition, a replaced connection epoch, or unmount cancels pending retries.

The configuration is `{ "media": { "webrtcOrigin", "readerUsername", "readerPassword" } }`, read
once at startup from two places in order, so credentials never enter the bundle. First the
same-origin `/runtime-config.json`: `pnpm dev` serves it from `SWEEP_MEDIA_WEBRTC_ORIGIN`,
`SWEEP_MEDIA_READ_USERNAME`, and `SWEEP_MEDIA_READ_PASSWORD`, answering 503 with any of them
unset, and a production host may serve the same JSON at that path. When that read yields no
complete configuration, the console reads the relay's copy at `GET <relay origin>/runtime-config.json`
with the relay bearer from its bootstrap (`src/media/runtime-config.ts`,
`relayMediaConfigurationSource`), where the relay origin is the bootstrap `baseUrl` with `ws`
mapped to `http` and `wss` to `https`; the relay serves the same three values from its own
environment, so a built `dist/` plays wherever it is hosted as long as its origin is listed in
the relay's `SWEEP_CONSOLE_ORIGINS`. With neither source the console runs with playback disabled
and says so on every live tile. The player files under `src/media/` come from PR #68 and will be
reconciled when it merges.

## Shell and modules

`src/tokens.css` holds the design tokens (colour, type, spacing, radii, shadows, motion,
breakpoints). `src/shell/` is the persistent frame: header with the network stop, state tags,
selection, control-authority line, connection pills and session sheet; rail and bottom tab bar;
the working pane with its sub-tab strip; the fleet context column; and the footer dock that shows
the one pending plan with its full Intent v1 envelope. The newest warning or info notice stays on
a line under the header row as a polite live region, the newest danger is the banner alert, and the
session sheet keeps the capped history. `src/modules/registry.ts` declares each
module (id, label, component, context renderer) in navigation order: Control, Live, Gesture,
Speech, Captures, Worlds, Devices, Map. Module selection lives in the shell and a pending
request survives switching. Modules the relay does not feed yet render an honest empty state.

## Devices module

`src/modules/devices/` lists every device the relay reports, connected and departed: class, unit
and device id, adapter, advertised capabilities, link, battery and position, control authority and
the safety operator, video state, sensor state (`no lidar` when the kit is not advertised, else
the last scan age), readiness reasons in the class's wording, and the last refusal that named the
device. Beside the list is the configuration a node needs to join: the relay URL from the console's
bootstrap (or a note that none was given), the session, and a device id, as a copyable block. The
device key is never shown; the relay never sends it, and a person enters it on the device.

## Fleet map

`Map` draws one canvas (`src/modules/map/FleetMap.tsx`) with ordered passes: the
relay's occupancy raster, the geofence box, each scanning device's short trail, its newest lidar
returns in that device's colour, and every device that reports a position as a heading triangle
labelled with its device id. The room frame is x east, y north, in metres; the canvas is y-down,
so every projection flips y exactly once (`projection.ts`). Dragging pans, the wheel zooms about
the pointer, the zoom buttons about the centre, and Fit view frames the geofence, or the placed
fleet when there is none.

The optional raster is requested from `GET /api/sessions/{id}/map` under the relay bootstrap URL read as HTTP,
behind the relay bearer, the same base and bearer the transcripts endpoint uses
(`src/relay/map-endpoint.ts`, `src/relay/origin.ts`). It is read once on mount and once a second
while the pane is mounted, and never after it unmounts. Image row 0 is the grid's maximum y and
`X-Sweep-Map-Origin-X`/`-Y` name the bottom-left cell corner, so the raster is placed from
`(origin_x, origin_y + height × resolution)`. Headers that do not describe a grid, a body that
does not decode, or a refusal draw no raster and say so. HTTP 404 means live occupancy is
unavailable; it does not establish whether a mapper is running. The current relay has no live
occupancy or reset route: the former implementation in PR #251 was closed, and the bounded,
registered live overlay belongs to #245. Survey candidates are separate immutable local evidence.
Without a bootstrap nothing is read. Production bootstrap does not provide a reset endpoint,
so `Reset map` is disabled. A future supported reset requires both an explicitly supplied
reset URL and a successful map read from the current endpoint; a successful GET alone cannot
enable it. Isolated test fixtures may supply that explicit reset URL.

Positions come from the relay's telemetry projection (`x`, `y`, and an optional `heading_deg`, or
`yaw_deg` from a node that names it that way), else from the pose of the device's newest scan in
the current connection epoch. A device that reports neither is named under the map rather than
placed, and a device with no heading is drawn as a circle rather than a guessed direction. Scans
and trails come from the sensor store's ring, never from the control reducer. The geofence is read
from the catalog's configuration snapshot, which the relay does not serve yet, so production draws
no box until real configuration is available.

Each scanning device keeps one hue by unit (`--color-scan-1` to `--color-scan-4`, beside
`--color-stream-*`); the map ground is `--color-map-grid`, the raster frame `--color-map-frame`,
the geofence `--color-map-geofence`. `LidarPolar` (`src/modules/map/LidarPolar.tsx`) plots the same
newest scan in the device's own frame with forward up, on every ground-vehicle registry card and
device card that advertises `lidar`.

## Control module

`src/modules/control/` is the Control and capture module from the v4 design: Swarm (selection
chips, fleet and motion controls, the translate pad, the formation panel), Capture (the three-step
flow, room field with inline validation, pattern cards, Capture room, the capture-readiness mirror,
the plan detail), Commands (the catalogue), Requests (lifecycle rows with a timestamp per state and
retry as a new intent with `retry_of`), and Fleet (registry rows and the departed list). `controls.ts` holds the pure gating and
geometry; every control builds its envelope through `control/intent.ts`, which now covers every
Appendix E name the contract lists. `takeoff`, `land`, `land_all`, `sweep`, `capture_room`, `survey_area` and formations park
in the dock until the operator confirms the exact envelope. Ground motion and webcam drafts
also require confirmation. A retry creates a new intent ID with `retry_of`; confirmation-gated
requests return to the dock and recheck the current targets, source, epoch and capability. The authoritative state projection carries the relay's
capability profile and exact enabled-intent list. Controls outside that list remain visible with
their reason but are disabled before preview or dispatch; the network stop remains universally
available by explicit safety policy. Missing or malformed capability metadata fails closed at the
WebSocket parser.
Public `capabilities`, `node_status` and `capture_readiness` events are parsed strictly and accepted
without degrading the connection. They do not independently grant control: authoritative state
frames continue to own command readiness. The capture guidance panel remains unreported until
its own supported projection is available.

## Catalog modules

Captures, Worlds, and Devices’ Health (Connectivity) and Config sections read
a `CatalogClient` from `src/catalog/`: captures, the building and its rooms, generation jobs,
per-node details, shared services, health metrics and configuration groups. The relay exposes no
endpoint for any of these yet, so production wires `UnreportedCatalogClient`: every surface reads
unreported and every action refuses with its reason. Relay-owned facts on
those pages (node membership, telemetry staleness, video, the two sockets, the pending plan) come
from the control state, never the catalog, and an apply-now configuration save invalidates a
pending plan through the control hook so the shell states it.

## Gesture and Speech modules

Gesture (`src/gesture/`, panel in `src/modules/gesture/`) is the webcam producer: tracking is off
until the operator enables it, then the browser asks for camera permission and the MediaPipe
GestureRecognizer runtime and model load from the MediaPipe CDN. In the default Capture/HOLD profile,
open palm drafts `capture_room`,
closed fist drafts `hold`, thumb up confirms and thumb down cancels a gesture-drafted preview; a
draft carries source `webcam` and is never sent until it is confirmed in the dock. Low confidence,
an interrupted dwell, a repeated pose, a denied permission, a dropped webcam, a model that fails to
load, and a refused webcam relay source are each shown as states that emit nothing, and a draft is
blocked while the console connection is not connected, because the roster and selection it would
be built from arrive on that connection. Network stop retains its button and keyboard path.
Each pose shows its own readiness and targets; confirmation retains the preview's exact targets
and connection epochs. Palm and thumb-up use a 0.60 score threshold, thumb-down 0.70, and fist
0.80. Pointing-up, Victory and I-love-you use 0.70 pending recordings of those poses. A candidate
needs 80% strong frames, a strong final frame and its full dwell (600 ms for drafts, 400 ms for
decisions). A 200 ms neutral release is required before confirmation or cancellation and between
every Flight action; repeated poses remain suppressed until neutral. Camera gaps cannot prove
dwell or release. Download session (JSONL) saves recognizer frames, strong-frame counts, policy
transitions, status changes and intent events. The stripped recorded-session replay stays in tests.

`Flight (opt in)` maps open palm to Arm session, pointing up to Takeoff selected, Victory to
Forward 0.5 seconds, closed fist to Backward 0.5 seconds, I love you to Land selected, thumb up
to confirmation, and thumb down to cancellation. The visible Flight buttons work with the camera
off. Every Flight action drafts and requires separate confirmation; retry parks again. Arm session
does not start motors. Pulses use each selected aircraft's body frame at ±250 mm/s for 500 ms,
require `body_pulse_v1`, and retain the selected global IDs. Duration is not a distance guarantee.
Robot targets block Flight actions before an intent is created; robot control follows the separate
class-aware controls and the relay's advertised support.

Both panes share the target strip (`src/modules/gesture/TargetStrip.tsx`): the selection count,
chips that toggle selection through the relay, All ready, the blockers line, and the design's quick
commands, each wired through the control hook: Hold, Takeoff and Land all draft a preview for the
dock and Come home sends at once; a name outside the relay's advertised capability set is listed as unsupported and remains disabled.

Speech (`src/voice/`, `src/speech/`, panel in `src/modules/speech/`) is push-to-talk through the
relay transcription endpoint: hold the button to record, release to upload; recording stops one
second before the relay's thirty-second cap. A relay plan is parsed strictly, bound to the current
session, correlation, state, capabilities, absolute expiry, plan digest, and deterministic step IDs,
then staged one step at a time with source `language`. Confirmation sends only the exact staged
payload, and any relevant state or input change invalidates it. A relayed transcript without such a
plan is display-only. Separately typed text may use the labelled local matcher for bounded ground pulses/return, `capture_room`,
`hold`, or `select`; local negation and ambiguity produce no draft. Without a relay bootstrap, and
without a configured real language service, the module reports it unavailable. The typed guide derives suggestions from current devices and capabilities; aircraft-only selection excludes ground robots. Ground HOLD is described as a zero-drive request.

The M2.0 control panel emits the production Intent v1 sequence for session arm, aircraft
selection, confirmed takeoff, configured-step translation, hold, come home, and confirmed
land-all. Takeoff and land-all stay in preview until the operator confirms the exact request,
selection, and roster version. The network E-stop remains available from both its button and the
separately authenticated keyboard connection.


### Mixed fleet controls and sensing

Control › Swarm chips add or remove one device from the current selection. **Only** selects
one device; **Select aircraft**, **Select robots**, and **Select all ready** select a class
or the full ready roster. The relay remains authoritative: a selection change invalidates
an older movement preview. The relay Intent v1 boundary accepts at most 32 configured device IDs per selection; measured physical operating capacity and model-specific qualification remain separate concerns.

Gesture starts with Capture / HOLD. **Fleet motion** is an explicit opt-in profile:
point up → north, Victory → east, closed fist → south, I love you → west, open palm → hold.
Each translation drafts one relay-configured step for selected aircraft only. **Swarm
formations** maps Pointing Up to the explicitly confirmed `formation_set {name: line}`, Victory to independently advertised formation_next, and open palm to hold. Thumb up confirms a
webcam draft; thumb down cancels it. Changing profile stops tracking and cancels the pending
preview. Arming, takeoff and landing are available only in the separate confirmed Flight profile; network stop remains manual. Gestures use the
same Intent v1 preview, selection invalidation and relay outcome path as manual controls.

Aircraft use the relay's configured translation frame. Translation and formation controls
refuse a selection containing robots; Ground provides bounded robot pulses and configured return.
Formation previews show anonymous aircraft slots only.
Formation controls require an advertised intent and a known shape contract. Mapped-line profiles permit only line; they do not enable column or cycling. C2 supports its documented shapes, while its simulator-only release restriction remains in force for real hardware. Unknown profile shapes stay unavailable.

Map reads the authenticated occupancy PNG and displays reported fresh robot LiDAR scans.
Each scoped ground robot has one LiDAR. Each aircraft has an owner-reported infrared depth/proximity sensor whose model/interface and readings remain unverified; it must not be presented as LiDAR.
After two seconds without a scan, the live overlay disappears and the device reports stale
coverage. The historical occupancy raster is retained by the relay. Devices without lidar
report unavailable coverage; no return, no hardware, or a stale feed never means clear.
Lidar samples a single plane and does not detect obstacles above or below that plane.

### Complete device telemetry

Devices, Live › Device inspection, and selected registry cards share the same diagnostics. They
retain the typed relay telemetry, node status and camera capability projections. Custom
`node_status.device_telemetry` groups appear as expandable readable rows; **Complete
reported telemetry** includes every reported field, connection epoch, and available scan.
Missing values say unreported. Bridge and capture advice show age and expire after five
seconds; old-epoch or reordered public node reports cannot replace current diagnostics or
grant selection/motion authority. Capture guidance is tied to the selected aircraft and
room; missing coverage never implies accepted coverage for other sectors.

Robot diagnostics name the actual guard blocker, motor/health report, scan age, coverage,
nearest return and calibration. Uncalibrated raw ranges get a sensor-frame plot with no
inferred robot heading; they cannot supply world-map points. Drone diagnostics include
virtual stick, watchdog, authority reason, phone battery/thermal, hardware/firmware, camera
support/storage/FOV, and all GPS/attitude/SDK key groups supplied by the adapter. Unsupported
peripherals remain explicit in the adapter's controls report; no nonexistent command path
is implied. Commands includes confirmed bounded forward/backward body pulses, gated by the
relay capability, selected aircraft's `body_pulse_v1` claim, arm state and airborne state.

The browser can inspect optional custom JSON within its bounded parser: 16 KiB, depth four including the root, at most 4096 values, 128 keys per object, 512 items per list and characters per string, 64-character snake_case keys, and finite JSON-safe numbers. This is display support. The deployed relay and node must explicitly implement the same field before any such data can arrive; a frontend contract does not extend the backend wire protocol.


### Connected device controls

The console includes capability-gated UI contracts for per-device robot peripherals and
single-aircraft camera operations. These controls require matching implemented relay and
node routes in the deployed build; they are not evidence that paused adapter/backend work
has been integrated. Missing route, capability, current status, or readiness remains unsupported
or unavailable. See [the modular integration guide](../docs/modular-fleet.md).

When the route is available, the UI targets one explicit connected device without changing
fleet motion selection, previews the bounded action, and binds confirmation to the device’s
current epoch. Disconnect, capability loss, or rejoin prevents confirmation. Robot neck/light/
speech/screen arguments and aircraft photo/gimbal arguments remain typed and bounded; no
arbitrary vendor command is exposed. Model-specific numeric arguments are not portable to
another vendor without an adapter contract and hardware evidence.

Robot LiDAR readiness never becomes an aircraft flight or camera prerequisite. A playable
camera feed does not promise panorama capture, gimbal control, or media retrieval.

## Known-map destination review — issue #143

Control › Navigate uses the authoritative selection, including each device's class and connection
epoch. It resolves accepted destination names and aliases, asks for clarification when names are
ambiguous, and refuses excluded, wrong-floor, unreachable, or unsupported destinations. Grounded
aircraft require a separate takeoff operation; navigation review never drafts capture, survey, or
formation jobs.

The runtime discovers authenticated platform services on the configured relay and injects the real
HTTP `NavigationClient` through `services.navigation`. Its catalog
must identify the accepted map, floor, world frame, approval, content hashes and geometry/navigation
versions, plus the authoritative motion configuration. A preview binds those inputs to the request,
roster, selected identities, per-device outcomes, routes, arrival slots and class-specific hold behavior.
The pane and existing dock show this frozen evidence. Changed inputs, expired evidence, cancelled
requests and replaced providers retire the review; delayed responses cannot recreate it.

The relay source implements catalog, name resolution, explicit destination compilation, durable
preview storage, confirmation revalidation and active-map selection. Catalogs come from the exact
current approved map and the loaded autonomy configuration. The console can request a read-only
review when the platform advertises review support, including typed capability refusals and unknown
route reachability. This does not widen C1/C2 motion capabilities. The shared Intent v1
`navigate {zone_id}` vocabulary is registered; generic transmission paths, model plans and retries
cannot bypass its frozen review. No runtime route or device evidence is generated.

Class-qualified route planning and execution remain under #144/#145/#249. Current previews report
`dispatchEligible: false` and confirmation reports execution unavailable. A static authoring map is
not a generated flight-clearance artifact. Updating the console alone does not update an older
running relay: an unupgraded relay, missing approved map or missing measured autonomy configuration
remains visibly unavailable. See [platform integration](../docs/platform-integration.md) and the
[navigation HTTP contract](../relay/NAVIGATION.md) for the exact software and deployment boundaries.

## Shared map authoring — issue #248

Map › Map authoring edits operator-supplied occupancy images and measured map metadata in the existing
console. Enter the actual resolution, bottom-left origin, map version, floor and registered `world`
frame before drawing, then supply metric units, creation provenance and measured registration
identity, residual and threshold before validation. The editor supports named zones and aliases, corridor centerlines and widths,
hand-measured flight heights and tolerances, geofences, static obstacles, no-fly polygons, and tags
with orientation, provenance, confidence, observation references and tape-verification evidence. Numeric vertex editing and undo
support precise corrections. A LiDAR occupancy plane does not establish aircraft clearance.

Local drafts can be exported and imported as `sweep-map-draft-v1`; this is an editor document, not the
published `sweep-world-bundle-v1` schema. Image bytes, dimensions and hashes are checked on import. Export local
work before leaving the Map module or closing the console. Switching between its Live observations
and Map authoring tabs retains the local draft. Imported documents cannot confer relay validation or
approval, and local edits invalidate previously displayed validation and approval evidence.

`services.mapAuthoring` is supplied by the real authenticated HTTP adapter for revision listing/loading,
saving, server validation, exact-revision approval, comparison, active-map selection and recording
associated tag observations. Server save runs the world-bundle validator and records its result;
invalid drafts may remain editable stored revisions but cannot be approved.
Saving returns an immutable revision/hash; validation and explicit audited approval must refer to that
same saved identity. Position and drive-over requests carry the exact saved bundle/revision/hash,
which must match the active approved map and the host-qualified registration. Responses and capture
receipts retain that reference; repeated map/floor labels cannot associate another map's evidence.
Drive-over tag recording names one selected ground robot and its current connection epoch;
another device's observation is rejected. The editor rejects late responses after edits or provider changes. Local checks
help correct geometry and evidence, but never replace the world-bundle validator or measured hardware
qualification. Verified live position overlays additionally require the provider's observation capability,
a matching session/map/floor, a current reported device epoch and fresh world-frame association.
Generic telemetry coordinates are not promoted into map observations.

The relay stores immutable versioned drafts, published bundles, validation receipts, approvals and
operator audit records in session-scoped SQLite. Loading and validating an unchanged approved
revision preserves its original immutable approval binding. Use **Use approved revision for
navigation** to select its exact approved revision independently of approval. Multiple approved
maps require this explicit choice; an edited selected map does not silently follow its new head.

Live overlays and drive-over recording are advertised only when the host has configured a qualified
world-pose producer with its actual device identity, credential, clock domain and approved-map
registration. They consume the shared observation envelope; legacy x/y is not substituted. Those
source measurements and the real Level 1 map remain hardware work under #243/#246/#247. The editor
uses the same workflow for a real replacement map. Synthetic bundle and image fixtures live only in
isolated tests; the operator runtime starts with real inputs or honest unavailable states.

Ground nodes that report canonical signed observations use Control → Ground, the opt-in Ground pulses gesture profile, or explicit typed `pulse forward`, `pulse left`, `pulse right`, and `return home` phrases. Pulses request 250 ms at 80 mm/s forward or ±350 mrad/s yaw; the preview shows exact parameters and requires confirmation. These parameters do not establish measured distance or angle. Selection requires a fresh accepted pose from the declared source on the current connection, followed by an authoritative ready snapshot with drive authority. Connection/source, selection, authority and freshness are checked again at confirmation and send. A retry creates a fresh preview. Configured return remains unavailable until the relay advertises it and resolves its own approved return route. Ordinary map Navigate review and the existing aircraft gesture profiles remain separate capabilities; an observation in `odom` never becomes a world map pose.
