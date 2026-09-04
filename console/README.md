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
between those sources, and no connection retries silently.
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

## Gesture tracking

The Gesture readout panel below the Control / Capture grid is a second input channel through the
same Intent v1 path as the buttons. Tracking is off by default. To enable it, expand the panel,
pick a camera, and click Enable tracking; the browser asks for camera permission, then the
MediaPipe GestureRecognizer runtime and model load from the MediaPipe CDN (`@mediapipe/tasks-vision`
WASM and `gesture_recognizer.task`). A denied permission, a dropped or unplugged webcam, or a model
that fails to load are shown as distinct states that emit nothing; the network stop and the
physical RC are unaffected.

| Gesture | Held for | Score | Action |
|---|---|---|---|
| Open_Palm | 600 ms | >= 0.8 | draft `capture_room` for the selected aircraft and the room field |
| Closed_Fist | 600 ms | >= 0.8 | draft `hold` for the current selection |
| Thumb_Up | 400 ms | >= 0.8 | confirm the pending gesture-drafted preview |
| Thumb_Down | 400 ms | >= 0.8 | cancel the pending gesture-drafted preview |

A draft appears in the plan preview with source `webcam` and is never sent until it is confirmed;
the `intent_id` assigned at draft time is kept through confirmation, and the confirmed intent leaves
through the `webcam` connection. Thumb gestures act only on previews that a gesture drafted. A held
gesture is accepted once and then suppressed until the hand returns to neutral; low confidence and
an interrupted dwell are shown and emit nothing. `estop`, `arm`, `takeoff`, and free-flight motion
are never gesture-emittable (`src/gesture/policy.ts`). Thresholds and dwell are constants in the same
module.

Download session (JSONL) saves the current session: a header line with the enabled pairs, then one
line per recognizer frame, policy transition, status change, and intent event (`draft`, `confirm`,
`cancel`, `blocked`), each with monotonic `t` and epoch `wall_t`. These recordings feed the gesture
eval fixtures. Clear recording discards the buffer; the buffer is bounded and reports dropped
entries.

The development fixture (`/?fixture=control`) includes a `webcam` fixture client, so gesture drafts
can be confirmed without a relay while the camera and model are real.
