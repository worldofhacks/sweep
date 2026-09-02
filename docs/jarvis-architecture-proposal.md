# Jarvis-layer architecture proposal (Koby, 2026-09-01)

Proposal for generalizing Sweep's architecture toward a broader "intent runtime for
autonomous vehicles" while keeping the current capstone scope (drones, Crazyflie,
frozen intent set) unchanged. Posted to DM for review; full text below is the exact
proposal to review against `docs/prd.md`.

## Core framing

Sweep is already an input-agnostic intent contract + autonomy/safety core + operator
console (PRD §0, §5.1). Voice is proposed as another compiler into the same intent
language, not a separate control system — consistent with PRD §5.1's rule that intents
are the only thing inputs may emit.

## Proposed changes

1. **Add `capabilities()` to the `SwarmAdapter` protocol** (PRD Appendix C), returning
   a capability descriptor (`position_control`, `velocity_control`, `camera`, `gimbal`,
   `gps`, `indoor_positioning`, `formation`, `max_speed`, `vehicle_type`) so future
   adapters (`MAVSDKAdapter`, `DJIAdapter`, `AutelAdapter`) can satisfy the same
   contract without hardcoding vehicle assumptions into the intent layer.

2. **Two voice paths for the language module** (PRD §5.10, Phase 5): a fast local
   command router for simple operational commands (`hold`, `stop`, `come_home`,
   selection, altitude step) that resolves without a frontier LLM call, plus the
   existing LLM plan-compiler path for complex/multi-step requests. Justified by the
   PRD's own latency split (e-stop <100ms, intent-to-motion <300ms, plan preview up to
   2s).

3. **Treat `sweep`/`formation`/future `inspect`/`search` as composed behaviors above a
   smaller universal primitive set** (`takeoff, land, hold, goto, move_relative,
   set_altitude, set_heading, return_home, follow_path, camera_capture, camera_aim,
   get_state`) rather than one-off planner cases, so the primitive surface is stable
   while composed behaviors can grow.

4. **World-grounding layer**: treat `resolve_selection()` / `resolve_location()` (PRD
   §4.5) as the seed of a semantic world model (telemetry + map + detections) so
   language commands like "send the closest drone to that doorway" resolve through the
   same deterministic tools already specified.

5. Explicitly *not* proposed for the current capstone window: expanding the frozen
   intent set (PRD §8.6 forbids this without a contract change, a test, and all three
   inputs updated), adding new adapters, or changing Phase 1–6 dates.

## Author's own stated constraint

"You don't need to change your current capstone to pursue this" — the claim is that
items 1 and 2 are low-cost, forward-compatible additions; items 3 and 4 are framing/
refactor suggestions for after the frozen intent set is proven, not before.
