# console

Capability area: Interaction. Milestone: M0 onward.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to shared contracts or safety-critical paths name one change owner and require cross-review.

The operator console: map, gesture readout, ledger, video mosaic, focus pane, attention promotion, health strip, and the language input with plan preview. A static web app; all state comes from the relay over WebSocket.

Stack: Vite, React, TypeScript, pnpm. Webcam hand landmarks come from MediaPipe Tasks.

    pnpm install
    pnpm dev        # http://localhost:5173
    pnpm lint
    pnpm test       # deterministic contract, reducer, client, and component tests
    pnpm build      # static files in dist/

M0's `swarm-gesture-console.html` (ten intents, dwell and confirmations, six-drone map sim, session recording, WebSocket intent emission) drops into `public/phase0/`. Vite serves it unchanged at `/phase0/swarm-gesture-console.html` while it is ported into components. First M1 job: point it at the relay instead of its internal sim.

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
`SWEEP_RELAY_ORIGIN` (default `ws://127.0.0.1:8000`), `SWEEP_SESSION_ID` (default `demo`), and
`SWEEP_RELAY_TOKEN` (no default). With the token unset it answers 503 with `{ "relay": null }` and
the console runs as before: visibly disconnected, network controls unavailable, no retry. The built
`dist/` contains neither the endpoint nor the token, so a production host must serve the same JSON
at that path or set the global itself. The `?fixture=` path never reads the endpoint.

The client opens `/ws/{session_id}` four times: one connection authenticates as `console` for
buttons and state, a separate connection authenticates as `keyboard` for the Shift+Escape network
stop, a third authenticates as `webcam` for the gesture producer, and a fourth authenticates as
`language` for exact relay-compiled plan steps. An intent is never moved
between those sources, and no connection retries silently. A relay that does not register the
`webcam` source refuses that connection; the Gesture module shows the refusal and emits nothing.
The token is sent only in the first WebSocket frame; it is never placed in a URL, rendered in the
UI, or included in console logging. Without the full bootstrap, the sources remain visibly
disconnected and network controls are unavailable.

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

## Camera dashboard

The Live module's walls and focus feed use the authoritative device ID, class, unit, connection
epoch, telemetry, membership, readiness reasons, and a closed media status with a last-frame
timestamp. The console derives the stream name from the class and unit, `drone{unit}` for aircraft
and `ground{unit}` for ground vehicles (`streamName` in `src/media/playback.ts`), and does not
render adapter-provided media URLs. `All devices` is the default for every roster: its responsive
wall has exactly one tile per reported device, adds new joins automatically, and keeps known
offline feeds visible. There are no empty hardware slots or six-device display cap; it supports
the configured four-aircraft/four-robot capacity. The aircraft walls and Ground pane remain
available as secondary views; the focus feed follows any device. With device IDs
`1,2,11,12,13` configured as two aircraft and three robots, the paths are `drone1`, `drone2`,
`ground1`, `ground2`, `ground3`; command envelopes retain the global IDs. One console uses one
authoritative relay/session; it does not combine rosters from separate relay instances.

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

For visual development only, `pnpm dev` may open `/?fixture=control`. The page displays a persistent
development-fixture banner, and the fixture is gated by Vite's `DEV` flag so a production build
cannot enable it. It is a UI/contract fixture, not acceptance evidence and not a flight simulator.

## Shell and modules

`src/tokens.css` holds the design tokens (colour, type, spacing, radii, shadows, motion,
breakpoints). `src/shell/` is the persistent frame: header with the network stop, state tags,
selection, control-authority line, connection pills and session sheet; rail and bottom tab bar;
the working pane with its sub-tab strip; the fleet context column; and the footer dock that shows
the one pending plan with its full Intent v1 envelope. The newest warning or info notice stays on
a line under the header row as a polite live region, the newest danger is the banner alert, and the
session sheet keeps the capped history. `src/modules/registry.ts` declares each
module (id, label, component, context renderer) in navigation order: Control, Live, Gesture,
Speech, Captures, Worlds, Devices, Reference. Module selection lives in the shell and a pending
request survives switching. Modules the relay does not feed yet render an honest empty state.

Fixture scenarios are data only and exist only in development builds: `/?fixture=control`,
`pending4`, `six6`, `down`, or `mixed` select a `FixtureRelayClient` scenario for the console,
keyboard and webcam sources and the matching `FixtureCatalogClient` tables. `mixed` reports two
aircraft beside three ground vehicles (ids 11 to 13, units 1 to 3; two with the lidar kit, one
docked without it) and emits one burst of synthetic room-shaped scans on the console source after
the first state frame (`syntheticRoomScan`). Production runs on the real relay WebSocket with no
fixture fallback.

## Devices module

`src/modules/devices/` lists every device the relay reports, connected and departed: class, unit
and device id, adapter, advertised capabilities, link, battery and position, control authority and
the safety operator, video state, sensor state (`no lidar` when the kit is not advertised, else
the last scan age), readiness reasons in the class's wording, and the last refusal that named the
device. Beside the list is the configuration a node needs to join: the relay URL from the console's
bootstrap (or a note that none was given), the session, and a device id, as a copyable block. The
device key is never shown; the relay never sends it, and a person enters it on the device.

## Control module

`src/modules/control/` is the Control and capture module from the v4 design: Swarm (selection
chips, fleet and motion controls, the translate pad, the formation panel), Capture (the three-step
flow, room field with inline validation, pattern cards, Capture room, the capture-readiness mirror,
the plan detail), Commands (the catalogue), Requests (lifecycle rows with a timestamp per state and
retry as a new intent with `retry_of`), and Fleet (registry rows and the departed list), plus the
Appendix E mission tracker under Reference › Mission. `controls.ts` holds the pure gating and
geometry; every control builds its envelope through `control/intent.ts`, which now covers every
Appendix E name the contract lists. `takeoff`, `land`, `land_all`, `sweep` and `capture_room` park
in the dock until the operator confirms the exact envelope; the rest send at once. A retry creates a new intent id with `retry_of` set. Takeoff, fleet landing and capture retries
return to the dock for fresh confirmation of the same arguments; other retries retain their
confirmation and send immediately. The authoritative state projection carries the relay's
capability profile and exact enabled-intent list. Controls outside that list remain visible with
their reason but are disabled before preview or dispatch; the network stop remains universally
available by explicit safety policy. Missing or malformed capability metadata fails closed at the
WebSocket parser.
Public `capabilities`, `node_status` and `capture_readiness` events are parsed strictly and accepted
without degrading the connection. They do not independently grant control: authoritative state
frames continue to own command readiness. The capture guidance panel remains unreported until
its own supported projection is available.

## Catalog modules

Captures, Worlds, and the Reference group's Health (Connectivity), Config and States sections read
a `CatalogClient` from `src/catalog/`: captures, the building and its rooms, generation jobs,
per-node details, shared services, health metrics and configuration groups. The relay exposes no
endpoint for any of these yet, so production wires `UnreportedCatalogClient`: every surface reads
unreported and every action refuses with its reason. The fixture scenarios carry the design's
tables through `FixtureCatalogClient` (`control` present but empty, `pending4` and `six6`
populated, `down` keeping the last snapshot while the console link is down and refusing actions);
job chains run on an injectable scheduler so tests advance them by hand. Relay-owned facts on
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
Download session (JSONL) saves the recognizer frames,
policy transitions, status changes, and intent events.

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
plan is display-only. Separately typed text may use the labelled local matcher for `capture_room`,
`hold`, or `select`; local negation and ambiguity produce no draft. Without a relay bootstrap, and
in fixture mode, the module reports language disabled and still accepts separately typed text.

The M2.0 control panel emits the production Intent v1 sequence for session arm, aircraft
selection, confirmed takeoff, configured-step translation, hold, come home, and confirmed
land-all. Takeoff and land-all stay in preview until the operator confirms the exact request,
selection, and roster version. The network E-stop remains available from both its button and the
separately authenticated keyboard connection.
