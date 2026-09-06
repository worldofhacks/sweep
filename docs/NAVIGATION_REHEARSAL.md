# Navigation browser rehearsal

The rehearsal opens a built console against an isolated relay and four signed simulated nodes. It selects one airborne aircraft, previews and confirms five destinations, verifies arrival against the previewed endpoint, checks that the other three aircraft remain stationary, and lands the fleet.

Run from the repository root after installing the Python and console dependencies:

```sh
pnpm --dir console build
node console/scripts/navigation-browser-smoke.mjs
```

The destinations come from the synthetic demo catalog: kitchen, lobby, the first formation destination, the second formation destination, and atrium. The test also cancels a preview and verifies that cancellation creates no navigation intent record. Every confirmed route goes through the HTTP preview endpoint, console confirmation, WebSocket intent path, planner/arbiter, signed node command, and fresh arrival telemetry.

Each run writes `output/playwright/navigation-browser-<timestamp>/` with route-preview screenshots, an evidence summary, relay audit files and the service log. The browser CI job runs this rehearsal and uploads those files on success or failure. Credentials are generated for the isolated process; the demo binds to loopback.

The nodes use kinematic simulation and the map is synthetic. This evidence covers software routing and command boundaries. Hardware activation still requires accepted map, localization, clearance, phone configuration and flight evidence.
