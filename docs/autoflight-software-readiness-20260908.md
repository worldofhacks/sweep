# Autoflight software readiness, 8 September 2026

This record maps each acceptance gate to the production call path and a test or evidence command that exercises it. The #94 observation consolidation passed independent review at `f8555fec`; CI on the eventual main merge remains pending. Field evidence remains outstanding for every physical acceptance item below.

## Track A: Ohmni, map, and ground localization

| Gate | Production path | Verification | Field acceptance still required |
| --- | --- | --- | --- |
| #247 | `adapters/ohmni/runtime.py` publishes the bounded scan/pose stream; runtime safety retains fault and stop evidence | `uv run pytest adapters/ohmni/test_runtime.py adapters/ohmni/test_runtime_safety.py` | Probe the actual robot, qualify the LiDAR mount/profile, and exercise stop behaviour |
| #78 | `tools/ohmni_live_tag_mapper.py` and tag-candidate fusion retain camera-linked tag evidence | `uv run pytest tests/test_ohmni_live_tag_mapper.py tests/test_ohmni_tag_candidate_fusion.py` | Install the tag grids, measure every tag and zone spacing, and retain the tape and Ohmni world ties. The current 53-tag record is provisional camera-fitted evidence, not a fully surveyed installation. |
| #99 | `relay/survey_area.py` owns the selected-ground-robot recording lifecycle and produces candidate evidence through `tools/ohmni_scan_record.py` | `uv run pytest relay/tests/test_survey_area.py` | Teleoperate one Level 1 run, save/reload/validate its candidate, and retain session, device, frame, clock, occupancy, pose, and tag evidence |
| #81, #82 | `tools/world_bundle.py`, `tools/map_geometry.py`, and `planner/navigation_deployment.py` bind map, route, geofence, and geometry artifacts | `uv run pytest tests/test_world_bundle.py tests/test_map_geometry.py planner/test_navigation_deployment.py` | Approve the measured Level 1 bundle, routes, and geofence |
| #243, #84 | `perception/world_localization_runtime.py` consumes admitted observations and `relay/control_localization.py` publishes the control projection | `uv run pytest perception/test_world_localization.py perception/test_control_localization.py relay/tests/test_control_localization.py` | Measure registration, validate live hand-carried localization, and retain calibrated clock and latency evidence |
| #94 | `POST /api/sessions/{session_id}/observations` authenticates a device-bound producer, then routes through `RelayRuntime.process_frame`, `RelaySession.process_observation`, and `ObservationIngress`. `WorldObservationService` stores only that admitted canonical record and projects its approved-map response. `tools/world_replay.py` reads canonical records and historical captures. | Independent review at `f8555fec`: `uv run pytest relay/tests/test_platform_observations.py relay/tests/test_platform_reference.py relay/tests/test_world_observation_guards.py relay/tests/test_ground_platform_navigation_execution.py tests/test_world_replay.py` (61 passed in 64.04 s) | Load the host-owned bindings, clock mappings, registrations, and measured map association for the deployed relay. Camera, localization, clock, and route performance require device evidence. |
| #246 | Ground pose, identity, and release gates flow through `relay/ground_navigation_identity.py` and ground navigation runtime | `uv run pytest relay/tests/test_ground_release.py relay/tests/test_ground_platform_navigation_execution.py` | Exercise local and remote ground stops against the qualified robot configuration |

## Track B: Mini 3 control, routing, and flight acceptance

| Gate | Production path | Verification | Field acceptance still required |
| --- | --- | --- | --- |
| #43, #85 | `FlightExecutor`, `DjiFlightPort`, watchdog/limits, and `AxisProbe` connect the bridge to virtual-stick control | `cd adapters/dji_mini3/pilot-app && ./gradlew :bridge-core:test :bridge-node:test` | Flight pin/session, hover, 15-minute control, axis/deadman, and RC-takeover evidence |
| #51 | DJI encoded frames flow through `WhipClient`, codec gate, and publication state machine to WHEP playback | `cd adapters/dji_mini3/pilot-app && ./gradlew :bridge-publish:test :bench:test` | End-to-end live-feed latency below 300 ms |
| #83 | `calibration/tag_intrinsics.py` and the checkerboard calibration API produce the intrinsics and latency evidence consumed by the bridge/localization path | `uv run pytest tests/test_calibration.py tests/test_calibration_quality.py tests/test_latency_evidence.py` | DJI checkerboard calibration and a 60-second latency record |
| #144 | `planner/navigation.py` produces clearance-checked, pinned routes and arrival slots | `uv run pytest planner/test_navigation.py` | Accepted measured geometry and later physical route trials |
| #145 | `planner/navigation_runtime.py` revalidates every segment; `planner/relay_bridge.py` connects confirmed execution to relay lifecycle evidence | `uv run pytest planner/test_navigation_runtime.py planner/test_relay_bridge.py` | Bench and hand-carried runbook evidence, then physical route acceptance under #86 |
| #19, #86 | Confirmed route execution is measured by `evals/flight_acceptance.py`, which binds five raw recording digests to a reviewed manifest | `uv run pytest tests/test_flight_acceptance.py`; `uv run python -m evals.flight_acceptance rehearsals.json --evaluation-manifest evaluation-manifest.json --output localization-software-report.json` | One-drone qualification and five approved named-route rehearsals |
| #97 | `relay/capabilities.py` is the shared advertised and enforced C1 profile across relay, planner, console, and language discovery | `uv run pytest relay/tests/test_capabilities.py` | Mini 3 deployment evidence from #19 and #18. This gate is independent of later multi-aircraft qualification. |
| #20, #18, #87 | Fleet execution and selected-land coordination run through planner/relay integration | `uv run pytest relay/tests/test_platform_multi_aircraft_navigation.py planner/test_navigation_runtime.py` | Second-aircraft, walking-skeleton, and formation qualification |

## #94 integration review

`relay.observations` is the shared production envelope for ingress, localization, the Android and Ohmni producers, audit, and replay. The map endpoint admits an authenticated observation through the normal relay path before applying its separate host registration and approved-map checks. The retained console response remains a map-position projection with its source, epoch, capture time, ingest time, confidence, reference, and frame association.

A localization world pose is kept separate from the adapter odometry identity used for ground readiness. `RelaySession.process_observation` changes that readiness identity only for an adapter principal. `ObservationIngress` also requires the configured producer role for each source binding. The independent test run above covers the map projection guards, the real positive HTTP world-pose route, ground execution confidence refusal, canonical and historical replay decoding, and the retained map/registration checks.

The independent review passed on `f8555fec`. Main-branch CI has not run for the merge commit.

Map authoring in #248 and navigation review in #143 are outside this record. They remain separate product work.
