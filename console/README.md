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

Production has no simulator or fixture fallback. The hosting shell must set an in-memory runtime
bootstrap before `main.tsx` runs:

```ts
window.__SWEEP_RELAY_CONFIG__ = {
  baseUrl: 'wss://relay.example.internal',
  sessionId: 'active-session-id',
  token: '<relay token supplied by the trusted local shell>',
}
```

The client opens `/ws/{session_id}` three times: one connection authenticates as `console` for
buttons and state, a separate connection authenticates as `keyboard` for the Shift+Escape network
stop, and a third authenticates as `webcam` for the gesture producer. An intent is never moved
between those sources, and no connection retries silently. A relay that does not register the
`webcam` source refuses that connection; the Gesture module shows the refusal and emits nothing.
The token is sent only in the first WebSocket frame; it is never placed in a URL, rendered in the
UI, or included in console logging. Without the full bootstrap, both sources remain visibly
disconnected and network controls are unavailable.

## Camera dashboard

The camera mosaic and focus pane are fixture-first. They use the authoritative aircraft ID,
connection epoch, telemetry, membership, readiness reasons, and a closed media status with a
last-frame timestamp. The console derives the display name `drone{id}` and does not render
adapter-provided media URLs. MediaMTX endpoints, credentials, recording, latency measurement,
and browser playback remain held for M3.1.

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

Fixture scenarios are data only: `/?fixture=control`, `pending4`, `six6`, or `down` select a
`FixtureRelayClient` scenario for both the console and keyboard sources.

## Gesture and Speech modules

Gesture (`src/gesture/`, panel in `src/modules/gesture/`) is the webcam producer: tracking is off
until the operator enables it, then the browser asks for camera permission and the MediaPipe
GestureRecognizer runtime and model load from the MediaPipe CDN. Open palm drafts `capture_room`,
closed fist drafts `hold`, thumb up confirms and thumb down cancels a gesture-drafted preview; a
draft carries source `webcam` and is never sent until it is confirmed in the dock. Low confidence,
an interrupted dwell, a repeated pose, a denied permission, a dropped webcam, a model that fails to
load, and a refused webcam relay source are each shown as states that emit nothing. `estop`, `arm`,
`takeoff`, and free-flight motion are never gesture-emittable (`src/gesture/policy.ts`). Download
session (JSONL) saves the recognizer frames, policy transitions, status changes, and intent events.

Speech (`src/voice/`, `src/speech/`, panel in `src/modules/speech/`) is push-to-talk through the
relay transcription endpoint: hold the button to record, release to upload; recording stops one
second before the relay's thirty-second cap. The transcript, or typed text, compiles locally to one
canonical intent (`capture_room`, `hold`, or `select`); every other recognised command is refused by
name, ambiguity returns options, and the outcome card says the local fallback ran. Drafting sends
nothing: the intent leaves on the console connection only after the dock confirms it. Without a
relay bootstrap, and in fixture mode, the module reports language disabled and still compiles typed
text.
