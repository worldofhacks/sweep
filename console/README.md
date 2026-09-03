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

The client opens `/ws/{session_id}` twice: one connection authenticates as `console` for buttons
and state, and a separate connection authenticates as `keyboard` for the Shift+Escape network
stop. An intent is never moved between those sources, and neither connection retries silently.
The token is sent only in the first WebSocket frame; it is never placed in a URL, rendered in the
UI, or included in console logging. Without the full bootstrap, both sources remain visibly
disconnected and network controls are unavailable.

For visual development only, `pnpm dev` may open `/?fixture=control`. The page displays a persistent
development-fixture banner, and the fixture is gated by Vite's `DEV` flag so a production build
cannot enable it. It is a UI/contract fixture, not acceptance evidence and not a flight simulator.

## Media bootstrap

The console fetches `/runtime-config.json` before rendering media. The response carries the WHEP
and HLS origins plus the read-only MediaMTX credential; those values are never compiled into the
Vite bundle. Build and serve the production assets with the runtime endpoint:

```bash
pnpm build
SWEEP_MEDIA_WEBRTC_ORIGIN=http://127.0.0.1:8889 \
SWEEP_MEDIA_HLS_ORIGIN=http://127.0.0.1:8888 \
SWEEP_MEDIA_READ_USERNAME=sweep-reader \
SWEEP_MEDIA_READ_PASSWORD="$SWEEP_MEDIA_READ_PASSWORD" \
pnpm serve
```

The development server exposes the same endpoint contract from those environment variables. A
missing or invalid endpoint leaves playback disabled while preserving the rest of the console. The
production server returns 503 from the runtime endpoint when media is not configured and continues
to serve the flight controls without exposing a partial credential.

`SWEEP_CONSOLE_BIND_HOST` and `SWEEP_CONSOLE_PORT` select the local listener.
`SWEEP_CONSOLE_ORIGIN` is the canonical browser-visible HTTP or HTTPS origin. Omit a protocol's
default port (`:80` or `:443`) because browsers omit it from the `Origin` header. The production
server accepts only that origin's Host authority and does not trust forwarded-host headers. Docker
Compose gives the exact same origin to MediaMTX for WHEP and HLS CORS. A trusted TLS proxy may
therefore preserve the public Host header while forwarding to a loopback listener on another port.
