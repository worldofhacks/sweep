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

The client opens `/ws/{session_id}` three times: one connection authenticates as `console` for
buttons and state, a separate connection authenticates as `keyboard` for the Shift+Escape network
stop, and a third authenticates as `webcam` for the gesture producer. An intent is never moved
between those sources, and no connection retries silently. A relay that does not register the
`webcam` source refuses that connection; the Gesture module shows the refusal and emits nothing.
The token is sent only in the first WebSocket frame; it is never placed in a URL, rendered in the
UI, or included in console logging. Without the full bootstrap, both sources remain visibly
disconnected and network controls are unavailable.

## Camera dashboard

The Live module's walls and focus feed use the authoritative aircraft ID, connection epoch,
telemetry, membership, readiness reasons, and a closed media status with a last-frame timestamp.
The console derives the display name `drone{id}` and does not render adapter-provided media URLs.
Recording and latency measurement remain held for M3.1.

## Live playback

Every wall tile whose stream the relay reports `live` plays it over WHEP in its own session, so
the Wall of 4 holds four concurrent sessions, and the focus feed plays the focused aircraft's
stream the same way; playback needs the page to have been served a media configuration, and every
other state is said in words. A player is torn down with its tile: when the pane changes, when the
console unmounts, and the moment the relay stops reporting the stream `live`, after which the tile
says `offline` with the age of the last frame the relay knew about. The relay's `video` field
(`relay/README.md`, "Membership and state fan-out") is the only source of that status; the console
never probes MediaMTX itself.

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
module (id, label, component, context renderer) in navigation order; module selection lives in the
shell and a pending request survives switching. Modules the relay does not feed yet render an
honest empty state.

Fixture scenarios are data only and exist only in development builds: `/?fixture=control`,
`pending4`, `six6`, or `down` select a `FixtureRelayClient` scenario for the console, keyboard and
webcam sources and the matching `FixtureCatalogClient` tables. Production runs on the real relay
WebSocket with no fixture fallback.

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
No relay event carries capture-readiness guidance yet, so the compass and gates render unreported.

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

The [four-aircraft input demo](../docs/FLEET_INPUT_DEMO.md) runs this console through
the production relay and signed fake nodes, with repeatable gesture and fleet
acceptance evidence.

Gesture (`src/gesture/`, panel in `src/modules/gesture/`) is the webcam producer: tracking is off
until the operator enables it, then the browser asks for camera permission and the MediaPipe
GestureRecognizer runtime and model load from the MediaPipe CDN. Open palm drafts `capture_room`,
closed fist drafts `hold`, thumb up confirms and thumb down cancels a gesture-drafted preview; a
draft carries source `webcam` and is never sent until it is confirmed in the dock. Low confidence,
an interrupted dwell, a repeated pose, a denied permission, a dropped webcam, a model that fails to
load, and a refused webcam relay source are each shown as states that emit nothing, and a draft is
blocked while the console connection is not connected, because the roster and selection it would
be built from arrive on that connection. `estop`, `arm`, `takeoff`, and free-flight motion are never
gesture-emittable (`src/gesture/policy.ts`). Download session (JSONL) saves the recognizer frames,
policy transitions, status changes, and intent events.

Both panes share the target strip (`src/modules/gesture/TargetStrip.tsx`): the selection count,
chips that toggle selection through the relay, All ready, the blockers line, and the design's quick
commands, each wired through the control hook: Hold, Takeoff and Land all draft a preview for the
dock and Come home sends at once; a name outside the relay's advertised capability set is listed as unsupported and remains disabled.

Speech (`src/voice/`, `src/speech/`, panel in `src/modules/speech/`) is push-to-talk through the
relay transcription endpoint: hold the button to record, release to upload; recording stops one
second before the relay's thirty-second cap. The transcript, or typed text, compiles locally to one
canonical intent (`capture_room`, `hold`, or `select`); every other recognised command is refused by
name, ambiguity returns options, and the outcome card says the local fallback ran. Drafting sends
nothing: the intent leaves on the console connection only after the dock confirms it. Without a
relay bootstrap, and in fixture mode, the module reports language disabled and still compiles typed
text.

The M2.0 control panel emits the production Intent v1 sequence for session arm, aircraft
selection, confirmed takeoff, configured-step translation, hold, come home, and confirmed
land-all. Takeoff and land-all stay in preview until the operator confirms the exact request,
selection, and roster version. The network E-stop remains available from both its button and the
separately authenticated keyboard connection.
