# Sweep Prior Art

Date: 2026-09-03. This document is the output of a deep open-source prior-art pass across the entire Sweep platform, run before continuing to build major components ourselves. Guiding rule: **reuse > adaptation > informed reimplementation > greenfield invention**, rejected only for licensing, maintenance, hardware, or architectural cost.

Method: full repo/issue/PR inventory first, then seven parallel research tracks (DJI/MSDK ecosystem, the WildBridge bridge family, swarm frameworks, indoor localization, obstacle avoidance and dynamic objects, networking/video, language/gesture control and architecture conventions). Funnel: **100+ projects discovered → ~45 cloned → 25+ read at source level → 20 classified ADOPT/ADAPT/REFERENCE with file-level targets.** Every hardware claim below carries an evidence class: **documented** (official docs), **claimed** (README/vendor), **tested** (field evidence: videos, papers, issues, logs, or measured in this pass), **inferred** (computed/judged).

---

## Executive Summary

1. **Sweep's biggest unknown is already solved in the wild.** Virtual Stick control of the DJI Mini 3 (non-Pro) through MSDK V5 is **documented by DJI** (supported since MSDK 5.3.0; officially the *only* automation path on Mini 3 — no waylines/POI, [dji-sdk#754](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/754)) and **field-proven by at least five independent parties**, including indoors without GPS on RC-N1 controllers ([Krucena](https://github.com/Robotics-DAI-FMFI-UK/Krucena), Comenius University: multi-Mini-3 indoor choreography, BODY-frame velocity Virtual Stick, vision positioning, Unlicense) and multi-Mini-3 outdoor fleets ([WildBridge](https://github.com/WildDrone/WildBridge)/[lyrebird](https://github.com/SDU-UAS-Center/lyrebird), SDU, RiTA 2025 paper; a third party confirms Mini 3 + RC-N1 "working perfectly" in [WildBridge#3](https://github.com/WildDrone/WildBridge/issues/3)).
2. **Do not build the Android bridge (#43) from scratch.** Seed it from the official MSDK V5 sample's ViewModels (MIT) plus the WildBridge/lyrebird lineage (MIT): its video path is *exactly* our stack (`ICameraStreamManager` → NV21 → WebRTC → **WHIP publish into MediaMTX**, ~2,850 lines of vendorable Kotlin, six-drone config included). Known deltas to fix, all localized: a metric-velocity endpoint, a command deadman (~300–700 ms → zero velocity; secondary timeout → **LAND**, never RTH indoors), telemetry push rate (WildBridge's real rate is 2 Hz despite the 20 Hz README claim — code-verified), and a Mini 3 control profile.
3. **The wider swarm ecosystem validates our thin custom core.** Every mature framework (Crazyswarm2, Aerostack2, MRS, XTDrone, EGO/Swarm-Formation) is ROS-native and aircraft-locked to Crazyflie/PX4; none can fly a Mini 3. The reusable value is algorithms and patterns, three of which are afternoon-to-two-day ports: a **Buffered-Voronoi-Cell velocity safety filter** for the arbiter, **gym-pybullet-drones' `VelocityAviary`** as a dynamics-true sim backend behind our existing adapter Protocol, and **XTDrone's MIT numpy consensus formation-keeping controller** (it already emits capped ENU velocity commands — our exact modality).
4. **The AprilTag localization decision (PR #66) survives scrutiny**, with two mandatory engineering additions: **EKF with delayed-measurement replay** fused with MSDK velocity (uncompensated video latency alone eats 12–28 cm of the 25 cm budget at 0.5 m/s) and **multi-tag joint PnP** with ambiguity gating. Detection compute is a non-issue (measured 12–33 ms/frame at 2.7K). Motion blur is the top physical risk. Every rival modality fails on cost, accuracy, or stock-hardware grounds; SLAM/VIO is REJECT for MVP (no synced IMU, no metric scale).
5. **The M3.0 clearance gate cannot be passed by the camera and should be restructured.** Three of the five protected directions are physically unobservable by the Mini 3's forward-only camera, and 20/20 clean trials only bounds a stochastic detector's miss rate at ≤13.9% (95% CI). The certifiable design: **static clearance = validated map + AprilTag pose + deterministic per-direction stopping-envelope check** (Nav2 Collision Monitor concepts; ~1.0–1.2 m inflation at 0.5 m/s), fail-closed on staleness; **dynamic intrusion = detection alert + operator confirmation + RC safety operator** (exactly #62's contract); monocular depth **advisory-only** (can hold, never clear).
6. **Video and networking plans are confirmed with numbers.** MediaMTX (MIT, 20k★) covers ingest/auth/recording for #51/#53/#21 outright; LAN WHIP→WHEP measures p50 180 ms / p95 240 ms, so the <300 ms budget is achievable but must be measured end-to-end early (OcuSync consumes ~100–200 ms first; DJI's RTMP path measures 4–5 s — avoid it for control-relevant viewing). WebSocket+JSON is quantitatively fine at our scale (6×25 Hz ≈ 150 msg/s); the real lessons are latest-wins conflation on state fan-out and 1 Hz heartbeats — which is precisely the class of bug already filed as #69/#70/#71.
7. **Our language/gesture safety architecture is ahead of the prior art** (nobody found combines a frozen data-only schema, preview+confirm, and independent deterministic revalidation). Cheap upgrades to port: TypeFly's policy-coverage test, structured-rejection→replan loop, a `clarify` schema alternative, and the N-of-M consensus + wake-gesture pattern for MediaPipe gestures (measured FP reduction 2.3/min → 0.02/min).
8. **Do not insert ROS 2; adopt its conventions.** Android bridges can't speak DDS (a second bus would be mandatory), DDS-on-WiFi multi-robot failure is documented, and the two enviable tools (rviz, rosbag) are obtainable on our existing JSON bus via **Foxglove + MCAP**. Adopt: an explicit ENU "arena" frame declaration, MAVLink's ack taxonomy (`IN_PROGRESS`, retryable-vs-terminal refusals), duration/relative on motion commands, heartbeat liveness.

Estimated build we no longer need to do ourselves: the WebRTC publisher, the media server, the tag detector, the detector/tracker stack, a dynamics simulator, and viz/replay tooling — several engineer-weeks replaced by integration work.

---

## Current Sweep Architecture

Source of truth: [docs/prd.md](prd.md) (v0.6). One-paragraph recap: an input-agnostic frozen **Intent v1** contract (buttons first; voice and webcam gestures as parallel lanes) flows through a FastAPI/WebSocket **relay** (authoritative state, fleet registry with roster versions + connection epochs, append-only JSONL audit), a deterministic **planner** (formations, sweep lanes, capture missions), a pure-Python **safety arbiter** (geofence, spacing, battery return, e-stop; no model in the safety path), and typed-Protocol **adapters** (deterministic sim first-class; DJI Mini 3 Android bridge planned in #43). Video via **MediaMTX** (WHEP primary), perception events never emit commands, and the room-world path (World Labs Marble) is presentation-only by rule. Explicitly no ROS ([PRD §4.3](prd.md)).

## Current Development Inventory

Full subsystem matrix as of 2026-09-03 (main + 10 open PRs + 35 open issues):

| Subsystem | Exists today | In flight | Not started | Major unknowns |
|---|---|---|---|---|
| Relay / intent bus | `relay/` complete for M1 scope | #55 hardening, #46 relay↔autonomy bridge | — | durability bugs #69/#70/#71 |
| Planner / arbiter | `planner/`, `arbiter/safety.py` (1,737 ln) | #46, #47 formations/sweep | map_area routing | disarm ownership |
| Adapters | sim (flight+camera) complete | #46 networked sim | DJI bridge (#43) | everything hardware |
| Console | M1.3 depth, camera dashboard | #72, #73/#74 redesign, #49 voice UI | later modules | — |
| Media | mediamtx.yml only | #68 (draft, +2.7k ln) | DJI publish (#51), 4-source (#67) | codec vs browser WebRTC |
| Localization | decision only (PR #66) | — | all code (#57, #58) | latency, blur, calibration |
| Clearance | — | — | all (#57) | gate is unpassable as worded (see below) |
| Perception | README stub | — | #62 | detector choice (resolved below) |
| Language | corpus merged (#45) | #41 (+5.9k ln, merge-held) | local fallback | real-provider evidence |
| Speech | — | #49 (merge-held), #75 | M4 hardening | — |
| Gesture | phase-0 prototype only | — | #38, #39 | thresholds policy |
| Room worlds | manual proof only | — | #59 (blocked on paid key) | observed API shapes |
| Evals/logging | JSONL audit + sim scenarios | #55 torn-write, #46/#41/#49 suites | replay UI | — |

Notable inventory facts: **zero TODO/FIXME markers in source** (all debt is tracked in issues); ~20k added lines sit in the 10 open PRs; merge-order deps #72→after #49, #47→after #46; and two strong prior-art docs already exist for the LLM/gesture boundary ([prior-art-intent-mapping.md](prior-art-intent-mapping.md)) and the EMG band ([prior-art-emg-band-direct-integration.md](prior-art-emg-band-direct-integration.md)) — the previously *un*-surveyed layers were infrastructure (bridge, transport durability, localization, clearance, perception, coordination math), which is what this pass covers.

## Recommended Architecture Changes

Ranked by leverage; each is expanded in its section below.

1. **Seed the #43 bridge from prior art** (official sample ViewModels + WildBridge/lyrebird video/discovery/safety pieces) instead of writing a fresh MSDK app. Add the four known deltas (velocity endpoint, deadman, telemetry rate, Mini 3 profile).
2. **Restructure the M3.0 clearance gate** (#57): certify static clearance deterministically from map+pose; move dynamic intrusion to the #62 detection/operator path; demote camera depth to advisory. Resolve the gate wording with its owner before building anything.
3. **Add an EKF with delayed-measurement replay** (tag fixes applied at capture time, re-propagated with MSDK velocity) to the #57 localization design, plus multi-tag joint PnP and blur validation. Without latency compensation the 0.25 m gate is arithmetically unreachable at 0.5 m/s.
4. **Add a BVC velocity safety filter** to the arbiter (reimplemented from the RA-L'17 paper; Crazyflie firmware as behavioral spec): turns the 0.8 m spacing rule from veto into deflect, O(n) per tick.
5. **Wrap gym-pybullet-drones `VelocityAviary` behind the existing `SwarmAdapter` Protocol** as a second, dynamics-true sim backend (downwash included) while keeping the kinematic sim for CI determinism.
6. **Declare frames and upgrade acks in the contract**: ENU "arena" + FLU body with an explicit `frame` field on Pose/Position (`planner/models.py:Position` is currently bare x/y/z); add `IN_PROGRESS`+progress and retryable-vs-terminal refusal classes (MAVLink `COMMAND_ACK` taxonomy); `duration`/`relative` on motion intents (Crazyswarm2); an explicit stream→primitive handoff call on the adapter Protocol (`notifySetpointsStop` analog); 1 Hz bridge heartbeats.
7. **Fix the relay's fan-out semantics with the MQTT QoS lesson**: latest-wins conflation for 10 Hz state (never queue unboundedly — the root of #70), acks/retries only for one-shots (already have), heartbeat liveness. Keep WebSocket+JSON.
8. **Adopt Foxglove + MCAP** (mirror the JSONL audit log to MCAP, ~50 lines) for rviz/rosbag-grade live 3D fleet view and replay scrubbing with zero ROS.
9. **Pin the perception stack** for #62: YOLOX-s (Apache-2.0) via ONNX Runtime + roboflow/trackers BoT-SORT with camera-motion compensation + supervision glue; 3–5 fps sampled frames on the laptop; events timestamped at frame-capture time.
10. **Add cheap safety rungs from the survey** (hours each): position-quality convergence takeoff gate (cflib pattern), graded geofence action ladder REPORT→…→LAND→STOP (Skybrush), tracking-error failsafe rung (MRS "commanded vs observed velocity divergence ⇒ hold, then land").

---

## DJI / MSDK

**Canonical reference: [dji-sdk/Mobile-SDK-Android-V5](https://github.com/dji-sdk/Mobile-SDK-Android-V5)** (SDK under DJI EULA; sample code MIT; current 5.18.0, 2026-05, ~quarterly cadence). Copy these sample classes rather than inventing abstractions — exact files:

- Registration/connection/product detect: `models/MSDKManagerVM.kt` (`SDKManager.init → registerApp → onProductConnect`).
- Virtual Stick: `models/VirtualStickVM.kt` + `models/BasicAircraftControlVM.kt` — enable VS → `setVirtualStickAdvancedModeEnabled(true)` → `sendVirtualStickAdvancedParam(VirtualStickFlightControlParam)` with `RollPitchControlMode.VELOCITY`, `YawControlMode.ANGULAR_VELOCITY`, `VerticalControlMode.VELOCITY|POSITION`, BODY or GROUND frame. The 5–25 Hz recommendation is verbatim in DJI's IVirtualStickManager doc — matching the PRD. `VirtualStickStateListener` is the control-authority signal (RC takeover).
- Takeoff/land: `BasicAircraftControlVM.kt` (27 lines; `KeyStartTakeoff`/`KeyStartAutoLanding` actions). Gotcha: **takeoff interrupts VS** — re-enable after success (dronemind re-arms ~3 s post-takeoff).
- Telemetry: KeyManager `listen()` (push), not polling. **Measured reality check** (lis-epfl flight logs, Mini 3 Pro/5.3.0): key cache freshness ~5 Hz for position/attitude, **~1–2 Hz for `KeyAircraftVelocity`**. Design consequence: derive velocity from position deltas where needed and budget the arbiter for ~5 Hz-fresh state; re-measure on Mini 3 + 5.18 before freezing the telemetry contract (feeds #43's acceptance).
- Camera: `CameraStreamDetailVM.kt` (`ICameraStreamManager` YUV frames + raw encoded listener), `LiveStreamVM.kt` (RTMP push — one call, but **measured 4.1–5.3 s first-frame latency on a live Mini 3** by lyrebird; use for smoke tests only, WebRTC WHIP for real viewing).
- Media download: `models/MediaVM.kt`.

**Mini 3 (non-Pro) capability matrix** (documented): supported since MSDK 5.3.0 as its own entry; Virtual Stick, KeyManager telemetry, camera/live stream, media download, simulator all available. **Not available**: wayline/waypoint missions, POI/intelligent missions ([#754](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/754), firmware limitation), obstacle avoidance under VS (enterprise-only feature; Mini 3 has downward sensing only), and any V5 panorama mission API for consumer aircraft — reinforcing the PRD's runtime-probe stance on `pano_360`. The **DJI Cloud API is REJECT for control**: DJI states consumer aircraft will not be supported; keep its DRC MQTT message shapes only as protocol design reference. RC-N1 is the *required* controller for third-party MSDK apps on Mini 3 (the DJI RC screen unit can't sideload) — our hardware choice is mandatory, not a compromise.

**Behavior when VS commands stop** (community-established, consistent across all serious implementations): the FC reverts to hover after a fraction of a second; there is **no FC-side deadman**. Every credible project runs a 10–20 Hz resend loop plus an app-side deadman (dronemind: 700 ms → zero velocity, 3.5 s → RTH). Ours must be: deadman → zero velocity, secondary → **LAND** (RTH is meaningless indoors without GPS home).

**Known MSDK issues relevant to us**: [#405](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/405) (Mini 3 at 20 Hz VS — SDK crash fixed in 5.11; still use a single sender thread), [#764](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/764)/[#795](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/795) (Mini 3 indoor yaw/pose estimation quirks — relevant to indoor heading), [#255](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/255) (Android 14 targetSdk crash, fixed later), [#747](https://github.com/dji-sdk/Mobile-SDK-Android-V5/issues/747) (native crash on RC-N-class setups).

**The axis-transpose hazard**: lis-epfl verified by GPS displacement probes that on Mini 3 Pro + MSDK 5.3 in GROUND+VELOCITY mode, `setPitch()` drives EAST and `setRoll()` drives NORTH — transposed from the naive mapping ([lis-swarm-app](https://github.com/lis-epfl/lis-swarm-app) `SwarmActivity.java:889-898`). Their response-monitor pattern (live least-squares fit of commanded vs observed velocity to catch wrong-frame execution) is worth copying. **We must run the same rotation probe on our exact Mini 3 + 5.18 combination, in BODY frame too, before any autonomy** (now part of Experiment 1).

## Multi-Drone Control

**The bridge family — our #43 seed (ADAPT):**

| Project | Evidence for our hardware | What to take |
|---|---|---|
| [WildBridge](https://github.com/WildDrone/WildBridge) (MIT, 55★, active; SDU/Bristol, EU-funded ~2027) | Mini 3 + RC-N1 **tested** third-party ([#3](https://github.com/WildDrone/WildBridge/issues/3)); 2×/3× Mini 3 fleets flown (RiTA 2025 paper); 10 drones at 32 Hz, <113 ms mean RTT benchmarked | The `webrtc/` package (WHIP→MediaMTX), HTTP/TCP/discovery servers, RC-stick manual-override latch (~30% deflection kills loops), two-computer `X-Safety-Token` authority; file map: VS ingestion `WildBridgeDefaultLayoutActivity.kt:2181-2420` + `DroneController.kt:501-753`, telemetry `server/TelemetryServer.kt`, video `webrtc/*`, Python client `GroundStation/Python/djiInterface.py` |
| [lyrebird](https://github.com/SDU-UAS-Center/lyrebird) (MIT, pushed 2026-09-03; WildBridge's successor, same team) | Mini 3 **field-tested** | Most-current codebase; MAVLink 2 + HTTP/TCP + WHIP; if we wanted zero app work, running lyrebird as-is and pointing the relay at its ports is the fastest credible path to first flight |
| [Krucena](https://github.com/Robotics-DAI-FMFI-UK/Krucena) (Unlicense) | Mini 3 non-Pro + RC-N1, **indoor, no GPS, multi-drone, tested** (videos) | The existence proof for our exact regime: BODY-frame VS including vertical VELOCITY on vision positioning; read before first indoor flight |
| [dronemind](https://github.com/myselfshravan/dronemind) (no license) | Mini 3 + RC-N1 + phone + **RTMP→MediaMTX**, author-tested | The right *size* (8 Kotlin files) and the deadman/OnboardSafety patterns; unlicensed ⇒ re-implement patterns, don't copy |
| [lis-epfl dji-swarm](https://github.com/lis-epfl/lis-swarm-app) (no license) | Mini 3 **Pro** + RC Pro, tested fleets | Highest lesson density: axis transpose, telemetry freshness, latch-based operator lockout (single-shot disables get re-armed by queued messages — same hazard class as relay retries), yaw-rate + host-side heading-hold P controller, one-source-of-truth drone identity, **AirLink bandwidth narrowing for multi-drone RF in one room** |

Known WildBridge deltas before it can fly our mission (all localized; verified in code): telemetry loop sleeps 500 ms (real rate 2 Hz — README's 20 Hz is wrong), `/send/stick` is *latched basic-mode stick positions* not metric velocities (the advanced-mode template to extract is `DroneController.gotoAltitude`'s 10 Hz loop, `:730-741`), **no GS-link-loss watchdog** (latched sticks keep flying — real hazard indoors), no Mini 3 control profile (falls back to Mavic 3E gains), and `res/xml/accessory_filter.xml` referenced by the manifest is missing from the repo (possible first-build failure; copy DJI's stock filter).

**Swarm frameworks surveyed (31; all ROS-native ones unadoptable wholesale):** [Crazyswarm2](https://github.com/IMRCLab/crazyswarm2) (MIT — best fleet-API reference: declarative roster YAML with per-type battery thresholds, `group_mask` broadcast, duration-based `GoTo` for synchronized arrival, `notifySetpointsStop` stream-handoff, `ConnectionStatistics` cumulative link counters), [Skybrush server](https://github.com/skybrush-io/skybrush-server) (GPL — our closest architectural sibling: a production no-ROS Python fleet server; steal the `GeofenceAction` ladder REPORT→RETURN→LAND→STOP, registries, `UAVDriver` batch-signal pattern as *patterns*), [MRS UAV System](https://github.com/ctu-mrs/mrs_uav_system) (BSD — escalating failsafe ladder with an odometry-innovation trigger: "estimator disagrees with prediction" as a first-class failsafe), [Aerostack2](https://github.com/aerostack2/aerostack2) (BSD — only framework with DJI platforms at all, but OSDK/PSDK = enterprise aircraft; even it cannot fly a Mini 3), [XTDrone](https://github.com/robin-shaun/XTDrone) (MIT — see Formation Control), [MADER](https://github.com/mit-acl/mader) (async commit/check deconfliction protocol; Gurobi ⇒ unadoptable), [mavsdk_drone_show](https://github.com/alireza787b/mavsdk_drone_show) (fleet-ops architecture rhymes with our relay; restrictive license). Notably, **no surveyed system does infrastructure-free indoor multi-drone on consumer hardware** — Bitcraze demos all assume Lighthouse/UWB/mocap installations. Sweep's niche is real, and our epoch-stamped acks + roster versions are *ahead* of everything surveyed.

## Networking

- **WebSocket+JSON: keep.** Arithmetic: 6 drones × 25 Hz × ~1 KB ≈ 150 msg/s — three orders of magnitude under a single asyncio server's LAN capacity. lis-epfl's MQTT experience quantifies the real lesson: connect-per-command cost ~220 ms (~4.5 Hz max); a persistent connection makes 20 Hz/drone trivial. What MQTT would have given for free must be owned at our app layer: **(a) latest-wins conflation** for the 10 Hz state fan-out (QoS-0 analog — a stalled client must never grow an unbounded queue; this is exactly bug #70), **(b)** acks/retries for one-shots (QoS-1 analog — our intent_id/`retry_of` already covers it), **(c)** heartbeat liveness + `connection_epoch` (Last-Will analog — epochs exist; add 1 Hz bridge heartbeats and a missing-N-heartbeats ⇒ `LossBehavior` rule).
- DJI's own Cloud API converged on the same shape (services/services_reply + osd fan-out + drc low-latency channel over MQTT) — validating the relay contract's structure. REFERENCE only (consumer aircraft unsupported).
- gRPC/zenoh: REJECT at this scale. Zenoh's existence is itself evidence *against* DDS-on-WiFi (see ROS 2 section).
- WildBridge's **seq-echoed-in-telemetry** trick (command returns a monotonic `seq`, telemetry echoes the last-reached `seq`) is the cheap way to make "target reached" unambiguous for long-running intents — worth adding alongside `IN_PROGRESS` acks.

## Video

- **[MediaMTX](https://github.com/bluenviron/mediamtx) (MIT, 20,018★, pushed daily): ADOPT — it closes #53's server side outright.** Verified in source: WHIP (RFC 9725) ingest + WHEP egress on `:8889`; three auth modes (`internal` per-path users with hashed credentials, **`http` webhook — the relay can be the authorizer**, `jwt`/JWKS); segmented fMP4 recording + playback API. No media-server code to write.
- **Android publish path (#51): vendor WildBridge's `webrtc/` package** (`SharedDJIFrameSource.kt` — one `ICameraStreamManager` frame listener fanned out to N capturers; `WhipPublisher.kt` — 437 lines of SDP/ICE/WHIP POST/DELETE with exponential-backoff reconnect and first-frame watchdogs; built on the maintained [GetStream/webrtc-android](https://github.com/GetStream/webrtc-android) libwebrtc, Apache-2.0). It ships a six-drone MediaMTX config titled for exactly our topology. The alternative one-call path (`LiveStreamVM` RTMP) is proven on Mini 3 by dronemind but measures 4–5 s — fine as a bring-up smoke test, wrong for the <300 ms budget. A raw-H.264 passthrough (skipping re-encode) is possible via `addReceiveStreamListener` but requires matching DJI's bitstream to WebRTC packetization — do not attempt inside the MVP window.
- **Latency evidence**: MediaMTX WHIP→WHEP LAN production benchmark p50 180 / p95 240 ms; generic WebRTC on wireless LAN <200 ms typical; RTSP player pipelines 500–2000 ms; MJPEG on LAN ~60–120 ms measured (but ~10× bandwidth — four 1080p MJPEG streams over Wi-Fi will fight control traffic; cap the fallback's resolution/fps). The <300 ms p95 gate is achievable with hardware H.264 encode (MediaCodec — libwebrtc's Android default) but OcuSync's ~100–200 ms comes first: **measure glass-to-glass end-to-end early** (Experiment 4).
- RF planning for four aircraft + Wi-Fi in one space: read lis-epfl's AirLink bandwidth-narrowing notes (40→20/10 MHz); video on 5 GHz / control on 2.4 GHz is already our plan — validate under load in Experiment 2.

## Localization

**PR #66's AprilTag choice survives scrutiny.** Everything else either can't run on a stock Mini 3, misses the 0.25 m p95 gate, or costs 10–100×. The engineering findings that must shape #57/#58:

- **Detection compute is a non-issue — video latency is the whole problem.** Measured in this pass (M4 Max, synthetic frames, `bench_apriltag.py`): AprilTag 3 at full 2.7K = 32.6 ms (decimate=1, 4 threads) and **11.8 ms at `quad_decimate=2`**, all four test tags down to 28 px found. Run detection on the full-resolution frame with `quad_decimate=2, nthreads=4` — pre-downscaling the image *loses* small tags; decimate only affects quad search, decode stays full-res.
- **Latency compensation is mandatory, not optional.** End-to-end fix age ≈ 250–550 ms (video + detect). Uncompensated at 0.5 m/s that is 12–28 cm of stale-position error — the entire budget. The documented fix (PX4 EKF2 delayed-fusion horizon, EKF-DH literature, [RosettaDrone#132](https://github.com/RosettaDrone/rosettadrone/issues/132) discusses exactly this on DJI): **buffer states, apply each tag fix at its capture time, re-propagate with MSDK velocity**. Residual error becomes latency-jitter × velocity (±100 ms → ±5 cm). This EKF also satisfies the 500 ms gap gate by construction: it dead-reckons through short dropouts and fails closed (hold) when both fix age and telemetry age exceed thresholds. **Timestamping is the hidden risk**: WebRTC frames share no clock with MSDK telemetry — calibrate glass-to-consumer latency per configuration (blinking-LED test), treat as a constant with a jitter bound, monitor innovation bias online.
- **Pose quality**: 36h11 at 16–20 cm is detectable to ~7 m at 2.7K (documented formula); published error data shows ~1–10 cm per fix in range, with **camera-yaw obliqueness the dominant error source** and single-tag IPPE ambiguity flips a real failure mode ([apriltag#71](https://github.com/AprilRobotics/apriltag/issues/71)). Mitigations to adopt: **multi-tag joint PnP against the surveyed map** (the [PhotonVision MultiTag](https://docs.photonvision.org/en/latest/docs/apriltag-pipelines/multitag.html) pattern) whenever ≥2 tags are visible; for single tags, err1/err2-ratio + surveyed-normal prior + Mahalanobis gating against the dead-reckoned prior; drop fixes below ~40 px or low `decision_margin`. Placement planner (#58) target: **≥1 tag within 4–5 m of every route point** (two where cheap), mixed sizes near precision zones.
- **Libraries**: [pupil-apriltags](https://github.com/pupil-labs/apriltags) (AprilTag 3 C bindings, prebuilt wheels) primary; `cv2.aruco DICT_APRILTAG_36H11` as the independent cross-check — which also settles the family question: **keep 36h11** (the upstream README prefers `tagStandard41h12` for ~1.5× range, but OpenCV doesn't ship it, so 41h12 only if the cross-check is ever dropped). [TagSLAM](https://github.com/berndpfrommer/tagslam) (now ROS 2, active 2026-01) as an *offline* tag-map refinement tool from a recorded walkthrough — no runtime ROS.
- **Motion blur is the top physical risk** (rolling-shutter skew is negligible at 0.5 m/s): indoor auto-exposure at 1/60 s smears 7–15 px at 0.5 m/s — enough to break corner refinement. Mitigate with staging light, speed caps during reacquisition, shutter lock if MSDK exposes it on Mini 3. Validation must measure detection rate vs speed (Experiment 5).
- **Calibration**: ChArUco sweep through the *exact* RC-N1→MSDK→MediaMTX pipeline at the localization resolution (`cv2.calibrateCamera`, RMS <0.5 px). Kalibr is camera+IMU-rig overkill here.
- **Wall vs floor tags — needs one reconciliation decision.** PR #66 chose wall-mounted (rationale: gimbal −90°…+60° can't see ceiling tags — correct). The newer 416 Congress system plan circulating in the team ("indoor_swarm_system_v2") uses **floor grids + wall tags at −50° gimbal**, which the gimbal also supports and which the accuracy literature slightly favors (near-nadir floor tags at short range beat oblique wall tags at distance). Everything in this section (EKF, MultiTag PnP, blur, calibration, density targets) applies identically to both; the tag-map format should carry per-tag mounting normal either way. Flagged in Open Questions.

## Mapping / SLAM

**REJECT for MVP; REFERENCE for Future — which supports the current plan.** Hard constraint: every credible VIO ([VINS-Fusion](https://github.com/HKUST-Aerial-Robotics/VINS-Fusion), [OpenVINS](https://github.com/rpng/open_vins), [Kimera](https://github.com/MIT-SPARK/Kimera-VIO)) requires a synchronized high-rate IMU that MSDK does not expose; monocular [ORB-SLAM3](https://github.com/UZ-SLAMLab/ORB_SLAM3) has no metric scale; [DROID-SLAM](https://github.com/princeton-vl/droid-slam)/[DPVO](https://github.com/princeton-vl/DPVO) need big GPUs and stay scale-ambiguous; [RTAB-Map](https://github.com/introlab/rtabmap) wants RGB-D/stereo/LiDAR. The only metric anchors we have are the tags and MSDK velocity — i.e., exactly the M3.0 design. A future hybrid (mono odometry scale-anchored by tag fixes) is a legitimate F-track item. **[D2SLAM](https://github.com/HKUST-Aerial-Robotics/D2SLAM)**: assumes per-drone Jetson + quad-fisheye + 400 Hz IMU + an ADMM comm layer — impossible on stock Mini 3; its transferable concepts are (a) camera-to-telemetry time offset as a first-class calibrated quantity, (b) explicit sync timeouts, (c) the observation that our *centralized* laptop estimator is legitimately simpler than their decentralized design because bandwidth/consensus problems vanish off-board.

The map-*construction* path (phone-LiDAR scan → point cloud → per-band occupancy grids, per the 416 Congress plan) is standard tooling: Polycam/Scaniverse export → [Open3D](https://github.com/isl-org/Open3D) crop/voxelize → hand-drawn glass polygons (phone LiDAR sees through glass — the plan's own #1 risk; wall tags on glass double as visual warnings). This is offline preprocessing, not a runtime SLAM dependency, and fits the M2.6 "validated map" discipline.

## Indoor Positioning (alternatives evaluated)

| Approach | Verdict | Why |
|---|---|---|
| Wall/floor AprilTags + MSDK-velocity EKF | **ADOPT** | <$50, no aircraft mods, meets gate with the additions above |
| Cheap IR-webcam triangulation ([Joshua Bird mocap-drones](https://github.com/jyjblrd/Mocap-Drones): four $5 PS3 Eyes + IR LED) | **ADAPT (secondary)** | Best-value independent *ground truth* for Experiment 5 and a single-room fallback; too much per-room rigging to be primary across 3–5 rooms |
| UWB (Bitcraze Loco / DWM3001) | REFERENCE (fallback) | A 15–30 g powered tag on a 248 g Mini 3 is feasible (strobe-kit prior art ≤15 g; ~40 g payload threshold) and direct Mini 3 + UWB + MSDK prior art exists (Krupáš 2025) — but ~20 cm RMS makes a 0.25 m **p95** gate marginal, plus 4× tag build/battery logistics |
| OptiTrack/Vicon mocap | REJECT | $5k–50k+, per-room camera trees — against the low-cost constraint |
| Marvelmind ultrasound | REFERENCE | ±2 cm *claimed*, ≤16 Hz, beacon on aircraft, vendor-locked; claims unverified |
| WiFi CSI / BLE AoA | REJECT | 0.3–1 m accuracy class — misses the gate outright |

## Obstacle Avoidance

**The M3.0 clearance gate, as worded, cannot be passed by any software on this aircraft — restructure it.** Three independent arguments (full analysis in the research pass):

1. **Geometry**: protected directions are forward/rear/lateral/up/down; the Mini 3's only camera faces forward (gimbal −90°…+60°); rear/lateral/up are unobservable. No model selection fixes a field-of-view problem.
2. **Physics**: total sensing→deceleration latency for any off-board camera loop is ≈0.65–1.35 s → a 0.4–0.8 m stopping envelope at 0.5 m/s. Feasible range-wise, but monocular depth's documented false-clear modes (glass — an open research problem with its own TPAMI dataset; textureless walls; thin obstacles; low light) are exactly what "no false-clear" forbids.
3. **Statistics**: 20/20 clean approaches bounds a stochastic sensor's true miss rate only at ≤13.9% (95% CI) — a 20-trial campaign can meaningfully verify deterministic pipeline plumbing, not a neural detector.

**Recommended restructure** (needs sign-off from the M3.0 owner — top open question):

- **Static clearance = deterministic geometry**: AprilTag-localized pose (separately gated at p95 ≤ 0.25 m / ≤500 ms) against the validated occupancy map, with per-direction velocity-scaled stopping polygons and **~1.0–1.2 m inflation from occupied cells at 0.5 m/s** (0.25 m loc error + 0.25 m staleness + 0.35–0.5 m abort envelope + aircraft half-extent + map resolution). Fail closed on staleness/map-version mismatch — the gate's own "before command dispatch" wording already describes pre-dispatch validation. Concepts (not code) from [Nav2 Collision Monitor](https://github.com/ros-navigation/navigation2): stop/slowdown/approach zones, `VelocityPolygon` speed-scaled zones, most-aggressive-wins arbitration, fail-safe defaults when a transform is unavailable. This is also the industry pattern (ISO 3691-4 AGVs): where no certified sensor exists, constrain the environment and keep a human in the loop — which is exactly our staged-empty-rooms + RC-safety-operator regime.
- **Dynamic intrusion = #62's contract**: detection event → ≤1 s feed promotion → operator confirmation → RC operator stop. A walking person covers 0.7 m during video latency alone; no off-board loop beats them — the human does. Certify alerting latency and logging, not detection perfection.
- **Monocular depth = advisory only**: [Depth-Anything-V2](https://github.com/DepthAnything/Depth-Anything-V2)-Metric-Indoor-**Small** (the only Apache-licensed metric-indoor checkpoint — Base/Large are CC-BY-NC; its metric head is literally sigmoid × max_depth constant) at 1–2 fps as a rehearsal-time **map auditor** (sustained "map says clear, depth says obstacle" ⇒ fail map validation). It may add holds, never grant clearance. The best existing DJI-camera-only avoidance project ([dronefreak's Tello PyDNet avoider](https://github.com/dronefreak/dji-tello-collision-avoidance-pydnet), Apache, active) is structurally a demo-grade steering heuristic on unitless thresholds — strong evidence camera-only DJI avoidance has never been shown beyond demos.
- **Escape hatch** if reviewers insist on literal independent sensing: infrastructure-side room cameras (~200 lines of OpenCV MOG2 background subtraction per staged room, all-direction gross-intrusion detection at ~100–200 ms) or a sensored aircraft class. Both cheaper than pretending monocular depth can certify.
- Rejected: dynamic occupancy grids (no range sensor), predictive collision avoidance (ORCA-class prediction of *humans* — redundant under operator-in-loop; note inter-drone deconfliction is separate and covered under Swarm Coordination), [UniDepth](https://github.com/lpiccinelli-eth/UniDepth) (CC-BY-NC), Apple Depth Pro (compute+license).

## Dynamic Object Detection

**Pinned stack for #62** (all licenses clean, all laptop-CPU-viable): **[YOLOX](https://github.com/Megvii-BaseDetection/YOLOX)-s** (Apache-2.0) via ONNX Runtime, person class, recall-tuned threshold, 640 input, 3–5 fps sampled frames per feed (~100–200 ms/frame CPU — meets the 1 s promotion budget); **[roboflow/trackers](https://github.com/roboflow/trackers)** BoT-SORT with `enable_cmc=True` (camera-motion compensation — a moving drone camera sheds IDs on plain IoU trackers; note supervision's built-in ByteTrack is deprecated in favor of this library); **[supervision](https://github.com/roboflow/supervision)** (MIT) for Detections/annotator glue and console overlays. Events timestamped at *frame capture time* and never emitting commands, per the PRD. [Ultralytics YOLO11](https://github.com/ultralytics/ultralytics) is the better-maintained detector but **AGPL-3.0 with network-copyleft reach over the whole service — choose deliberately**; the Apache path is equivalent for person detection. Evidence note: aerial-view detection collapse (YOLO-IHD: stock models drop to 35% mAP at 30–45 m altitude) does *not* apply to our regime — eye-height flight in small rooms is near-COCO conditions; the residual risks are blur, low light, and door-edge partial views, which the #62 recorded-footage eval set should target. Open-vocabulary search (OWL-ViT / Grounding DINO, per the 416 Congress search demo and [future-perception doc](future-perception-manipulation-mapping.md)) rides the same event bus later; YOLOX covers the fixed-class MVP.

## Swarm Coordination

- **Inter-drone deconfliction — ADAPT (reimplement): Buffered Voronoi Cells.** The Crazyflie firmware's [`collision_avoidance.c`](https://github.com/bitcraze/crazyflie-firmware/blob/master/src/modules/src/collision_avoidance.c) (Preiss, from Zhou/Wang/Bandyopadhyay/Schwager RA-L'17) projects each commanded velocity into the drone's buffered Voronoi cell given peer positions, with an explicit sidestep rule for head-on-swap deadlocks. Velocity-native (our modality), O(n) per tick, afternoon-scale numpy for 4–6 drones. GPL ⇒ reimplement from the paper using the C file as a behavioral spec. Insertion point: the arbiter, between planner output and dispatch — it turns the 0.8 m spacing check from veto into deflect. (The 416 Congress plan names ORCA for this role; BVC achieves the same guarantee with far less machinery at our headcount, and the hard-stop backstop stays either way.)
- **Downwash-aware separation — ADAPT (math only)**: [Swarm-Formation](https://github.com/ZJU-FAST-Lab/Swarm-Formation)'s `swarmGradCostP` uses an **ellipsoid distance (a=2 vertical, b=1 horizontal)** against peers' *predicted* trajectories — vertical separation counts half, so drones don't stack in downwash. Two lessons port to Python in a day: the ellipsoid metric for our spacing rule (249 g aircraft stacked indoors is a real hazard — lis-epfl flags it too), and checking separation against *predicted* positions, not instantaneous ones. The full stack (ROS 1, GPL, MINCO optimizer) is REFERENCE.
- **Fleet lifecycle**: our roster_version/connection-epoch design is ahead of everything surveyed; add Crazyswarm2's cumulative `ConnectionStatistics`-style link counters (counters age better than instantaneous link-quality scores) and cflib's **kalman-variance-convergence takeoff gate** (refuse takeoff until position quality is stable for N seconds — `__wait_for_position_estimator` pattern).

## Formation Control

- **ADAPT: [XTDrone](https://github.com/robin-shaun/XTDrone) `coordination/formation_demo/follower_consensus.py`** (MIT): a ~40-line numpy consensus P-controller — per follower, sum offset-corrected position deltas to local leaders, add avoidance repulsion, norm-cap, emit **capped ENU velocity commands at 30 Hz**. Byte-for-byte our command modality; strip rospy and it drops into `planner/controller.py` as the between-replans formation-*holding* layer our deterministic slot placement currently lacks (slots position drones; this holds them under disturbance and latency). Its `formation_dict.py` (formations as N×3 offset arrays) matches our formation tables.
- **Slot assignment**: universally Hungarian across the ecosystem (Swarm-Formation ships a 351-line munkres header; XTDrone task_assignment; F.5 already names it). Use `scipy.optimize.linear_sum_assignment` — one line, already-available dependency, matches the 416 Congress plan.
- Formation-as-shape (Swarm-Formation's graph-Laplacian similarity metric — swarm keeps *shape* while deforming) is elegant REFERENCE material for post-MVP; rigid offsets + consensus holding is the right MVP scope.

## Trajectory Planning

Our sampled-setpoint approach (planner emits clamped velocity steps) is what every surveyed stack degrades to at the actuator boundary — and the only thing Virtual Stick accepts. Three upgrades when needed, in order:

1. **Carrot/pure-pursuit tracking + A\* on the band grid with string-pulling** (the 416 Congress plan's design) is standard and fine; lis-epfl's yaw-rate + host-side heading-hold P controller (anti-windup + deadband; ANGLE mode is "visibly choppy") is the proven heading pattern on this SDK.
2. **Duration-parameterized motion** (Crazyswarm2 `GoTo{goal, yaw, duration, relative}`): makes multi-drone synchronized arrival trivial — adopt the *shape* in Intent/adapter contracts even while tracking stays velocity-based.
3. **Smooth min-jerk trajectories**: if ever needed, port MINCO from **[GCOPTER](https://github.com/ZJU-FAST-Lab/GCOPTER) (MIT)** — header-only Eigen, ~150-line Python port for the closed-form single-axis case — not from the GPL Swarm-Formation stack. Post-MVP.
4. Sweep-lane/coverage planning: bespoke lawnmower is fine for rectangular rooms; [Fields2Cover](https://github.com/Fields2Cover/Fields2Cover) (BSD-3, Python bindings) only if concave rooms or lane-angle optimization start to matter.

## Safety

Findings that harden the existing arbiter/watchdog design (no architecture change needed — the survey confirms "no model in the safety path" plus deterministic checks is ahead of most research code):

- **Command deadman lives in the bridge** (300–700 ms → zero velocity; secondary → LAND). The FC provides none. WildBridge's current lack of one is its single most dangerous gap for indoor use.
- **Manual takeover must be a latch, not an event**: WildBridge's RC-stick latch (~30% deflection kills autonomous loops until explicitly cleared) + lis-epfl's 1 Hz re-disable (a queued enable can re-arm after an operator disable — same hazard as relay retries, which #69 already fixes in another guise).
- **Graded responses**: adopt Skybrush's `GeofenceAction` ladder (REPORT → RETURN → LAND → STOP) as an arbiter enum; adopt MRS's **tracking-error rung** (commanded-vs-observed velocity divergence ⇒ hold, then land — also our detector for the axis-transpose class of bug, via lis-epfl's response-monitor pattern).
- **Preflight gates**: position-estimator convergence before takeoff (cflib pattern); per-drone open-loop rotation probe before autonomy (lis-epfl).
- **Two-computer authority** (WildBridge `X-Safety-Token`): direct prior art for keeping e-stop authority segregated from the pilot path — maps onto our arbiter-owned stop semantics.

## Simulation

**ADOPT: [gym-pybullet-drones](https://github.com/utiasDSL/gym-pybullet-drones)** (MIT, 2.1k★, active, pip-installable, no ROS). `VelocityAviary` takes per-drone velocity setpoints (our exact modality) with real dynamics including **downwash** (`examples/downwash.py`) — wrap it behind the existing `SwarmAdapter` Protocol as a second backend (1–2 days) and the whole planner/arbiter eval suite gains tracking lag, overshoot, and interaction physics for free. Keep the kinematic sim as the fast deterministic CI target; PyBullet runs become the pre-hardware confidence tier (e.g., validating the 0.8 m spacing rule under crossing paths). CF2X URDFs are Crazyflie-scale — scale mass/inertia toward 249 g for realism, and treat absolute dynamics as indicative. All heavier simulators (AirSim/Colosseum — archived; Flightmare, RotorS — stale; Pegasus — Isaac Sim GPU stack) are REJECT for our need. Crazyswarm2's `TimeHelper` (same script runs sim or hardware at any timescale) is the pattern our adapter split already follows — keep it that way.

## ROS 2

**Recommendation: do not insert ROS 2. Adopt its conventions and its two best tools' equivalents.** This is a clear call, not a coin flip:

1. The boundary ROS 2 would improve doesn't exist: Android bridges can't speak DDS, so phone→relay stays WebSocket/JSON regardless — inserting ROS 2 between relay and autonomy means **two buses plus a bridge** serving three Python processes that share a repo and a frozen JSON contract.
2. **DDS-on-WiFi multi-robot is a documented failure mode** and our LAN is the failure scenario: SPDP multicast discovery "very unreliable on WiFi" (arXiv 2508.11366); practitioner reports of second-robot network collapse (Fast-DDS #4948); IGMP-snooping drops on consumer APs (ROS Discourse #28516). The community's fixes (Discovery Server, `rmw_zenoh`) are workarounds we'd adopt on day one.
3. Team tax: colcon, IDL codegen duplicating the frozen contract, QoS matrices, Linux-container dev on macOS — against a 3-person team with fast pytest suites.
4. The reuse case is weaker than it looks: `pupil-apriltags` needs no `apriltag_ros`; Nav2 is a 2D ground stack (its *concepts* are already harvested above); and rviz/rosbag value arrives via **Foxglove Studio + MCAP** on the existing JSON bus (~50-line JSONL→MCAP mirror) — ADOPT that instead.
5. Precedent: WildBridge ships ROS 2 as an *optional edge adapter* around an HTTP/TCP/JSON core; lis-epfl uses no ROS at all. That is the ecosystem pattern for DJI-bridge fleets.

Revisit triggers: Crazyflie hardware actually joining the fleet (the existing `adapters/crazyswarm2` stub then becomes one ROS-speaking edge process — keep the stub), team growth, or a hard dependency on a ROS-only package. Conventions to adopt now regardless: **ENU "arena" world frame + FLU body, declared via a `frame` field on Pose/Position** (REP-103/105; convert to DJI conventions only inside the bridge, where MAVROS does its ENU↔NED flip); `_deg`-suffixed degree fields (already our habit); dual timestamps (`t_bridge` at MSDK callback + relay ingest `t` = free link-latency metric); MAVLink `COMMAND_ACK` taxonomy (`IN_PROGRESS` + progress for `capture_room`/`survey_area`/`map_area`, retryable vs terminal refusals); PX4's minimum-stream-rate rule (setpoint stream <N Hz ⇒ hold).

## Natural Language / Agent Control

**Verdict: Sweep's chain (frozen data-only schema → preview+confirm → independent deterministic revalidation) is ahead of everything surveyed** — no found system does all three. This extends the conclusions of [prior-art-intent-mapping.md](prior-art-intent-mapping.md) to the runtime-engineering layer. Portable upgrades, all cheap, aimed at PR #41 and issues #37–#39:

1. **Policy-coverage test** ([TypeFly](https://github.com/typefly/TypeFly), Apache-2.0 — the best-engineered open LLM-drone system; its AST-allowlist + argument-clamping PlanPolicy trust boundary matches our arbiter stance word-for-word): add a test asserting every `IntentName` has an arbiter rule and a clamp entry, failing when an intent lands without safety coverage (their `test_skill_policy_coverage.py`).
2. **Structured rejection → bounded replan**: feed the arbiter's machine-readable refusal codes back into the compiler prompt for one retry before surfacing (`llm_planner.py: build_replan_feedback`).
3. **`clarify` as a schema alternative** (question string instead of a plan) so ambiguity never becomes a guessed selection — consistent with the PRD's clarification rule.
4. **Clamp at compile AND arbiter**: encode min/max in the JSON schema (vendor-enforced for free) *and* recheck deterministically — the schema is enforced by the model vendor, not by us. Two 2025-26 findings: heavy output constraints measurably degrade generation quality (keep the plan schema flat/small), and schema conformance ≠ semantic safety.
5. **Few-shot/eval corpus as versioned data files** beside the frozen schema, with adversarial classes (ambiguous, out-of-vocabulary, unsafe, compound, expected-refusal); [PromptCraft-Robotics](https://github.com/microsoft/PromptCraft-Robotics) (MIT) is a seed corpus, and preview+confirm is its explicit safety stance too — 2023-era consensus, not our idiosyncrasy.

**Gesture** (#38/#39, ADAPT [kinivi/tello-gesture-control](https://github.com/kinivi/tello-gesture-control), Apache-2.0): the load-bearing patterns are the **`GestureBuffer` N-of-M consensus filter** (fire only when ≥N−1 of last N frames agree; clear on fire = built-in refractory), a **terminal-action latch** (LAND blocks further commands), and a **wake/arming gesture** (measured spurious-activation reduction 2.3/min → 0.02/min). For the MVP window prefer MediaPipe's **Gesture Recognizer task** (canned classes with confidence) + N-of-M smoothing over training custom classifiers; kinivi's CSV-logging + notebook workflow is the fallback for custom vocabulary. Eval metric for #39: per-gesture precision/recall **plus false activations per minute on negative footage** (people moving naturally) — the literature-standard number. Risky intents keep console confirm; a gesture e-stop gets a maximally distinctive pose with a shorter consensus window (idempotent, so a rare FP is acceptable by design).

---

## Repository Matrix

The load-bearing subset (fuller per-candidate detail lives in the sections above; stars/dates as of 2026-09-03).

| Repository | License | ★ | Last push | Hardware evidence | Role for Sweep |
|---|---|---|---|---|---|
| [dji-sdk/Mobile-SDK-Android-V5](https://github.com/dji-sdk/Mobile-SDK-Android-V5) | MIT sample / DJI EULA SDK | ~1k | 2026-06 | Mini 3 documented | Bridge skeleton (copy the VMs) |
| [WildDrone/WildBridge](https://github.com/WildDrone/WildBridge) | MIT | 55 | 2026-08 | Mini 3+RC-N1 tested (issue #3, papers) | Bridge seed: webrtc/, discovery, safety latch |
| [SDU-UAS-Center/lyrebird](https://github.com/SDU-UAS-Center/lyrebird) | MIT | 3 | 2026-09-03 | Mini 3 fleets tested | Most-current bridge codebase; fastest-path option |
| [Robotics-DAI-FMFI-UK/Krucena](https://github.com/Robotics-DAI-FMFI-UK/Krucena) | Unlicense | 2 | 2025-10 | **Mini 3+RC-N1 indoor no-GPS tested** | Indoor existence proof; read before first flight |
| [myselfshravan/dronemind](https://github.com/myselfshravan/dronemind) | none | 0 | 2026-07 | Mini 3+RC-N1 author-tested | Deadman/bridge-shape patterns (unlicensed — no copying) |
| [lis-epfl/lis-swarm-app](https://github.com/lis-epfl/lis-swarm-app) | none | — | 2026-08 | Mini 3 **Pro** fleets tested | Gotcha list: axis transpose, telemetry freshness, lockout latch, RF planning |
| [bluenviron/mediamtx](https://github.com/bluenviron/mediamtx) | MIT | 20,018 | 2026-09-03 | n/a | Media server: WHIP/WHEP/auth/recording (#51/#53/#21) |
| [GetStream/webrtc-android](https://github.com/GetStream/webrtc-android) | Apache-2.0 | 881 | active | n/a | Maintained Android libwebrtc (transitive) |
| [pupil-labs/apriltags](https://github.com/pupil-labs/apriltags) | MIT | — | active | measured this pass: 12 ms/frame @2.7K | Primary tag detector |
| [AprilRobotics/apriltag](https://github.com/AprilRobotics/apriltag) | BSD-2 | — | active | — | Reference detector + pose ambiguity machinery |
| [berndpfrommer/tagslam](https://github.com/berndpfrommer/tagslam) | GPL | — | 2026-01 | — | Offline tag-map refinement tool |
| [utiasDSL/gym-pybullet-drones](https://github.com/utiasDSL/gym-pybullet-drones) | MIT | 2,121 | 2026-08 | n/a | Dynamics sim backend (`VelocityAviary`) |
| [robin-shaun/XTDrone](https://github.com/robin-shaun/XTDrone) | MIT | 1,714 | 2025-08 | PX4 SITL | Consensus formation controller (numpy, ENU velocity) |
| [bitcraze/crazyflie-firmware](https://github.com/bitcraze/crazyflie-firmware) | GPL-3 | 1,537 | active | Crazyflie | BVC collision filter — behavioral spec (reimplement) |
| [IMRCLab/crazyswarm2](https://github.com/IMRCLab/crazyswarm2) | MIT | 252 | 2026-08 | Crazyflie | Fleet-API reference: roster YAML, duration-GoTo, setpoint handoff |
| [skybrush-io/skybrush-server](https://github.com/skybrush-io/skybrush-server) | GPL-3 | 124 | 2026-08 | PX4/CF shows | No-ROS Python fleet-server architecture; geofence ladder (patterns only) |
| [ZJU-FAST-Lab/Swarm-Formation](https://github.com/ZJU-FAST-Lab/Swarm-Formation) | GPL-3 | 563 | 2024-01 | research quads | Ellipsoid separation + Laplacian shape metric (math only) |
| [ZJU-FAST-Lab/GCOPTER](https://github.com/ZJU-FAST-Lab/GCOPTER) | MIT | 1,293 | 2023-10 | research quads | License-clean MINCO donor (future smooth trajectories) |
| [Megvii-BaseDetection/YOLOX](https://github.com/Megvii-BaseDetection/YOLOX) | Apache-2.0 | 10,603 | 2025-06 | — | Person detector (#62) |
| [roboflow/trackers](https://github.com/roboflow/trackers) | Apache-2.0 | 3,745 | 2026-09 | — | BoT-SORT + CMC tracker (#62) |
| [roboflow/supervision](https://github.com/roboflow/supervision) | MIT | 49,864 | 2026-09 | — | Detections/overlay glue (#62) |
| [DepthAnything/Depth-Anything-V2](https://github.com/DepthAnything/Depth-Anything-V2) | Apache (Small only) | 8,757 | 2026-03 | — | Advisory-only map auditor |
| [typefly/TypeFly](https://github.com/typefly/TypeFly) | Apache-2.0 | 113 | 2026-07 | Tello | Validator/policy/replan patterns for #41 |
| [kinivi/tello-gesture-control](https://github.com/kinivi/tello-gesture-control) | Apache-2.0 | 343 | 2023 | Tello | Consensus buffer + latch patterns for #38 |
| [ros-navigation/navigation2](https://github.com/ros-navigation/navigation2) (Collision Monitor) | mixed | 4,657 | active | ground robots | Clearance-zone concepts (no code) |
| [HKUST-Aerial-Robotics/D2SLAM](https://github.com/HKUST-Aerial-Robotics/D2SLAM) | — | — | — | Jetson+fisheye rigs | Concepts: latency as calibrated quantity |
| Foxglove Studio + [MCAP](https://mcap.dev) | MPL/Apache | — | active | n/a | rviz/rosbag equivalents on the JSON bus |

## Adopt / Adapt / Reference / Reject

- **ADOPT**: MediaMTX; pupil-apriltags + cv2.aruco cross-check; gym-pybullet-drones (`VelocityAviary` backend); YOLOX-s + roboflow/trackers + supervision; Foxglove + MCAP; official MSDK sample ViewModels; scipy Hungarian for slots; WebSocket+JSON (own design, kept deliberately).
- **ADAPT**: WildBridge/lyrebird bridge pieces (webrtc/ package, discovery, telemetry server, safety latch — with the four fix-ups); XTDrone consensus formation controller; BVC filter (reimplement from paper); Swarm-Formation ellipsoid-separation math; kinivi gesture buffer/latch patterns; TypeFly policy/replan patterns; Depth-Anything-V2-Small (advisory only); IR-webcam triangulation rig (ground truth); dronemind patterns (re-implemented — unlicensed).
- **REFERENCE**: Krucena, lis-epfl dji-swarm, Crazyswarm2, Skybrush, MRS, Aerostack2, cflib, GCOPTER, EGO-Swarm/v2, MADER, Nav2 concepts, ISO 3691-4 discipline, D2SLAM concepts, TagSLAM (offline tool), DJI Cloud API shapes, PromptCraft, SayCan, RosettaDrone, MAVLink/MAVSDK/REP-103 conventions, UWB (fallback), Marvelmind.
- **REJECT** (with reason): ROS 2 as middleware (two buses, DDS-on-WiFi); DJI Cloud API for control (consumer aircraft unsupported); all SLAM/VIO for MVP (no synced IMU / no metric scale / GPU); mocap (cost); WiFi CSI/BLE AoA (accuracy class); UniDepth & DA-V2 Base/Large (CC-BY-NC), Apple Depth Pro (compute/license); dynamic occupancy grids & predictive human-collision avoidance (no range sensor; operator-in-loop makes them redundant); HaishinKit/StreamPack (no WHIP); AirSim/Colosseum/Flightmare/RotorS/Pegasus (archived/stale/heavy); MSDK-V4-era bridges incl. RosettaDrone for running code (no Mini 3); ultralytics YOLO11 *by default* (AGPL — flag for deliberate decision, not silently).

## Implications for Existing PRs

| PR | Recommendation | Basis |
|---|---|---|
| #41 language compiler | **Continue unchanged; add follow-ups** — policy-coverage test, structured-rejection replan, `clarify` schema alternative, schema-level clamps | TypeFly/PromptCraft findings; design already ahead of prior art |
| #46 button-to-sim gate | **Continue unchanged** | Core custom logic validated by survey — no framework replaces it |
| #47 formations/sweep | **Continue; post-merge add** XTDrone consensus holding layer + `linear_sum_assignment` slots; Fields2Cover only if rooms get concave | Formation Control section |
| #49 push-to-talk | **Continue unchanged** (+#75 timeout) | Commodity plumbing observation only; posture is right |
| #55 relay hardening | **Continue unchanged**; note for the future that the bespoke JSONL WAL re-derives SQLite-WAL-class guarantees — if a fourth durability bug appears, consider swapping the substrate rather than patching again | Inventory §8 |
| #56, #73 docs | Unaffected | — |
| #66 AprilTag decision | **Merge, then amend via #57/#58**: add the EKF delayed-measurement-replay requirement, multi-tag joint PnP, blur validation, density target; **resolve wall-vs-floor placement against the 416 Congress v2 plan** (gimbal supports both; floor tags slightly favored by accuracy literature) | Localization section |
| #68 media path | **Continue — approach confirmed**: MediaMTX auth/recording covers the rest of #53 server-side; for #51 go straight to the WHIP publish path (vendored WildBridge webrtc/) rather than RTMP-first (4–5 s measured) | Video section |
| #72 console reorg | Unaffected (merge after #49 as planned) | — |

## Implications for Existing Issues

| Issue | Current approach | Prior art | Recommendation |
|---|---|---|---|
| #43 bridge bring-up | Smallest fresh DJI app | WildBridge/lyrebird/Krucena/dronemind | **Change implementation**: seed from sample VMs + WildBridge pieces; add velocity endpoint, deadman→LAND, telemetry-rate fix, Mini 3 profile, axis probe, telemetry-freshness measurement; acceptance list unchanged |
| #51 DJI feed → MediaMTX | RTSP/WHEP via frame listener | WildBridge webrtc/ (proven, six-drone config) | **Reuse upstream code**: vendor the package; RTMP only as smoke test |
| #53/#21 media auth/slice | Per-path auth, WHEP, recording | MediaMTX `http` webhook auth, fMP4 recording | **Confirmed**; relay-as-authorizer is the cleanest fit |
| #57 localization + clearance gate | Wall tags + 5-direction sensing gate | PhotonVision MultiTag, EKF-DH, Nav2 CM, ISO 3691-4 | **Split the gate** (static=map+pose deterministic; dynamic=#62 path; depth advisory); add EKF replay + joint PnP + blur validation to the localization half |
| #58 tag placement | Visibility heuristic from validated geometry | Range formula + published error data | **Add targets**: ≥1 tag in 4–5 m everywhere, 2 where cheap, mixed sizes at precision zones, ≤45° incidence; resolve floor-vs-wall first |
| #62 detection events | Detector TBD | YOLOX/trackers/supervision | **Adopt the pinned stack**; frame-capture timestamps; recorded-footage eval incl. blur/low-light/door-edge cases |
| #63 map_area traversal | Collision-checked routes | Nav2 inflation discipline | **Add the number**: ~1.0–1.2 m occupied-cell inflation at 0.5 m/s, velocity-scaled per direction |
| #69/#70/#71/#75 relay bugs | Individual fixes | MQTT QoS split, broker lessons | **Continue #55 path** + add latest-wins conflation and 1 Hz heartbeats as the systemic fix for the class |
| #38/#39 gesture | MediaPipe producer + evals | kinivi patterns, wake-gesture data | **Adopt**: Gesture Recognizer canned classes, N-of-M consensus, wake gesture, terminal latch; FP/min on negative footage as the eval metric |
| #60 watchdog/evidence | Presence + reports | cflib/MRS/Skybrush | **Add rungs**: convergence takeoff gate, tracking-error failsafe, geofence action ladder |
| #29/#61 fleet membership | Registry lifecycle | Crazyswarm2 counters | Add cumulative link counters to telemetry |
| #17/#44/#59/#25/#24/#18–#20/#52/#64/#65/#67/#74 | — | — | **Unchanged** (#59 remains the M1-exit blocker — a credential, not a research problem) |

**New issues this research warrants** (proposed, not filed): BVC velocity filter in the arbiter; gym-pybullet-drones adapter; Foxglove/MCAP mirror; frame/units declaration on `Position` + `IN_PROGRESS`/retryable acks + stream-handoff call (one contracts issue); bridge validation experiment (below); AGPL stance decision.

## Proposed MVP Architecture

Same boxes as today — the change is what fills them:

```
MSDK V5 sample VMs + WildBridge-derived pieces   ←  ADAPT (was: greenfield app)
        │  (velocity endpoint, deadman→LAND, 10 Hz resend, Mini 3 profile)
        ├── WHIP ──► MediaMTX (auth via relay webhook, fMP4 recording)   ←  ADOPT
        │                └── WHEP ──► console          └──► perception:
        │                                                   YOLOX-s + BoT-SORT + supervision  ← ADOPT
        │                                                   DA-V2-Small advisory map audit    ← ADAPT
        └── WS/JSON telemetry + commands (heartbeats, conflated fan-out)  ←  KEEP (hardened)
                │
        Sweep relay ── planner (+ XTDrone consensus hold, scipy Hungarian)   ←  KEEP + ADAPT
                │            └── arbiter (+ BVC filter, map+pose clearance,
                │                 geofence ladder, convergence & tracking-error rungs)  ← KEEP + ADAPT
                ├── localizer: pupil-apriltags (+cv2.aruco check) → EKF w/ delayed replay  ← ADOPT libs, custom fusion
                ├── adapters: kinematic sim (CI) ∥ gym-pybullet-drones (dynamics) ∥ DJI bridge  ← ADOPT backend
                └── observability: JSONL audit ──mirror──► MCAP ──► Foxglove   ←  ADOPT
```

**What we no longer build ourselves**: media server, WebRTC publisher, tag detector, detector/tracker, dynamics simulator, viz/replay tooling. **What stays deliberately ours** (survey-validated): intent contract, relay state machine, deterministic planner, arbiter, fleet registry — nothing surveyed replaces them for this hardware, and several patterns (epoch-stamped acks) are ahead of the field.

## Proposed Future Architecture

Unchanged boxes, upgraded internals, in likely order: MINCO smooth trajectories (GCOPTER port) feeding the same velocity adapter; formation-as-shape (Laplacian metric) for deformable formations; mono odometry scale-anchored by tags (ORB-SLAM3-class) once a recorded-dataset eval justifies it; UWB as localization redundancy if tag dropouts bite; open-vocabulary search (Grounding DINO-class) on the #62 event bus; the `adapters/crazyswarm2` stub as the single ROS-speaking edge process if Crazyflies ever join; Fields2Cover lanes; predictive avoidance only with a sensored aircraft class. The ROS 2 decision gets revisited only on its named triggers.

## Immediate Experiments

Derived from the research (not the generic list); each kills a specific residual uncertainty:

1. **E1 — Bridge validation on the exact stack** (1–2 days, feeds #43/M1.9): build the seeded APK (register DJI app key; restore `accessory_filter.xml` if the build fails) → bench with props off: VS engage on Mini 3+RC-N1 (`flightMode: VIRTUAL_STICK` appearing answers the headline question), measure real per-key telemetry freshness on 5.18, test vertical VELOCITY vs POSITION → netted flight: 10 Hz velocity pulses, deadman test (stop sending mid-motion), RC takeover latch, **axis/rotation probe** (0.3 m/s pulses vs phone-reported attitude, BODY frame) → **indoor no-GPS probe**: VS enable + zero-stick hover stability on vision positioning at 1–2 m over textured floor — the one regime none of the bridge family has flown; go/no-go for off-board velocity control indoors.
2. **E2 — Multi-node scale + RF**: 2 then 4 bridge nodes against one relay; command RTT/jitter/drops per node; AirLink bandwidth narrowing + Wi-Fi channel plan under simultaneous video.
3. **E3 — Command-loop latency budget**: server → LAN → bridge → VS → observed motion (phone high-speed video or telemetry echo); fills the ms-numbers DJI doesn't publish and calibrates the clearance envelope math (currently conservative bounds).
4. **E4 — Video glass-to-glass**: WHIP path p50/p95 vs the <300 ms gate; blinking-LED timestamp calibration (the number the localizer EKF consumes); MJPEG/HLS fallback measured under 4-stream load.
5. **E5 — Tag localization dry run** (feeds #57): intrinsics through the live pipeline (RMS <0.5 px) → latency distribution (p95 <500 ms, jitter <150 ms) → static accuracy at 8–10 surveyed points, 1–6 m, 0–45° incidence (go/no-go p95 <0.15 m within 4 m; flip-rejection working) → dynamic taped-route run at 0.5 m/s with the full EKF (p95 ≤0.25 m, zero unhandled gaps — a dress rehearsal of the actual M3.0 gate) → blur curve (detection rate vs speed; <80% at 0.5 m/s ⇒ lighting/shutter/speed response). Optionally cross-checked by the $50 IR-webcam rig as independent ground truth.

## Open Questions

1. **M3.0 gate wording** (#57): does its owner accept the static/dynamic split? The current text can be read as pre-dispatch map validation (passable) or independent 5-direction sensing (not passable on this aircraft). Highest-priority decision; blocks clearance work.
2. **Wall vs floor tags**: PR #66 says wall-mounted; the 416 Congress v2 plan says floor grids + wall tags. One decision, then #58's planner targets follow.
3. **AGPL stance**: is ultralytics' network-copyleft acceptable for an open-source Sweep, or do we standardize on the Apache stack (recommended default: Apache)?
4. **Bridge seed choice**: WildBridge fork vs lyrebird fork vs sample-plus-vendored-pieces — E1 build experience should decide (lyrebird is most current; WildBridge has the RC-N1+Mini 3 report; both MIT, same lineage).
5. **`VerticalControlMode` indoors**: VELOCITY (Krucena/dronemind) vs POSITION (lis-epfl claims no velocity channel — contradicted by code elsewhere); E1 resolves.
6. **Telemetry freshness on 5.18**: if velocity keys are still 1–2 Hz-fresh, the EKF leans harder on position deltas — measure before freezing Telemetry v1 hardware fields.
7. **#59 World API key**: still the M1-exit blocker; purely operational.
8. **dronemind licensing**: patterns are re-implemented here, but if closer reuse ever looks attractive, contact the author first.
