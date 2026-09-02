# console

Capability area: Interaction. Milestone: M0 onward.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to shared contracts or safety-critical paths name one change owner and require cross-review.

The operator console: map, gesture readout, ledger, video mosaic, focus pane, attention promotion, health strip, and the language input with plan preview. A static web app; all state comes from the relay over WebSocket.

Stack: Vite, React, TypeScript, pnpm. Webcam hand landmarks come from MediaPipe Tasks.

    pnpm install
    pnpm dev        # http://localhost:5173
    pnpm lint
    pnpm build      # static files in dist/

M0's `swarm-gesture-console.html` (ten intents, dwell and confirmations, six-drone map sim, session recording, WebSocket intent emission) drops into `public/phase0/`. Vite serves it unchanged at `/phase0/swarm-gesture-console.html` while it is ported into components. First M1 job: point it at the relay instead of its internal sim.

PRD: sections 4.2, 5.8.
