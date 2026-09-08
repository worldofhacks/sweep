# Archived ground telemetry work — not a deployment branch

This branch preserves the uncommitted ground telemetry and peripheral work from the
older `codex/ground-control-telemetry` checkout at `e3f2d05`, captured on
2026-09-07 (America/Chicago). It is retained for selective migration, not execution.
The currently integrated implementation is `codex/unified-fleet-control` / PR #321.

The archive uses an earlier node protocol. Its ground join and custom status frames
are incompatible with the current registry/status parser. Do not merge this branch
wholesale, deploy its launchers, or replace current motion guards with these older
implementations. The source snapshot has not been newly hardware-qualified.

Useful remaining migration areas include richer battery/sensor/encoder telemetry;
neck, lights, speech and screen controls; a local stop/spotter interface; two-camera
publisher lifecycle; and LiDAR device/health discovery, ownership and reconnection.
The archived aircraft camera-control path also lacks complete Android integration.
Older direct encoder polling, sparse LiDAR clearance and aircraft-shaped ground
readiness are superseded by the current field runtime.

No private runtime configuration, credentials, device captures, logs or Android APKs
are part of this snapshot. Fake devices and fixtures in this archive are development
artifacts, not inputs to the operator console. Preserve real-device-only deployment.
