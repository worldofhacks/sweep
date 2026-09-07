# Sweep production-chain readiness

At the 11:41 UTC checkpoint on 2026-09-07, the field relay remains an observer:
its deployed `relay.app:app` entry point refuses commands because it has no
downstream executor. The published software has not been merged into main.
A composed deployment and a supervised vertical flight profile are being prepared.
Ohmni 11 is online with motion disabled. Ohmni 12 is offline; the operator suspects
a depleted battery. The aircraft and controller are being powered down for a
battery swap and charging.

The next physical milestone is a bounded ground mapping run. It requires usable
camera views, uninterrupted wheel odometry, collision sensing, a proven local
stop, and an operator-defined route. A partial tag layout can support a diagnostic
recording. The operator reports that tag placement is nearly complete. World
registration and flight approval require the measured tag and geometry evidence
described below.

## Observed field behavior

The deployed relay accepts the authenticated aircraft and ground identities and
retains their observations in distinct local frames. These frames have no measured
registration to the building's world frame.

| Check | Observed result | What remains unproven |
| --- | --- | --- |
| DJI connection | SDK registration and product connection succeeded; encoded video and battery telemetry reached the relay. | Replacement aircraft identity, guarded axes, deadman, and RC takeover. Before the battery swap, the operator enabled control authority and RC-operator readiness. Position quality remained zero because the current producer uses a valid GPS fix as its quality source; this does not establish that indoor hover is unavailable. |
| Browser playback | After the ground runtime restart, one browser session decoded all three feeds for one minute: D-01 at 1280×720; G-11 and G-12 at 640×480. Six samples each showed three connected peers, advancing video, and no console errors. | Sustained latency, recovery drills, calibrated capture time, and long-duration acceptance. |
| Ohmni-11 camera | A bounded Camera0/HAL capture produced 235 complete 1280×960 Gray8 frames at 30 fps; tag 18 was detected in every frame. The operator identifies this as the robot facing tag 18, away from the hallway. | Optical calibration, camera-to-body orientation, and tag-pose accuracy. Camera content alone does not establish chassis heading. |
| Ohmni-12 camera | Its downward camera detected floor tag 17 in 5 of 30 raw frames without preprocessing. The operator identifies Ohmni-12 as the hallway-facing robot and tag 17 as the takeoff starter tag. | Reliable floor-tag visibility through the chosen production camera mode, plus measured pose and size for tag 17. |
| Wheel odometry | Encoder replies were observed, but a measured reply gap of about 0.64 seconds exceeded the runtime's 0.35-second continuity bound. | A continuous recording suitable for mapping. This observation does not establish a mechanical wheel fault. |
| Ground control | Runtime software and its refusal/stop paths have automated coverage. The field launch keeps motion disabled. | Physical drive, braking distance, collision-stop behavior, link-loss stop, and a qualified return corridor. |

The Camera0 check temporarily paused only Ohmni-11's camera publisher. The local
preview app was closed, its capture socket removed, and the publisher restored.
The check issued no motor or camera-aim command. Phone wireless debugging was
disabled after setup; the phone's normal Wi-Fi and controller USB connection
remain the application transport.

The verified console build is `7019e68ed759ebc0b49bf89dead224575bd1c28d`. Its
production HTML and JavaScript hashes matched the staged build. The playback
check reported 315 decoded aircraft frames, 212 G-11 frames, and 221 G-12 frames.
It covers that observation interval only.

Those playback statistics were captured at 10:16:49 UTC. A separate screenshot
from 10:17:03 UTC shows both ground nodes disconnected. Both robot runtime logs
reported a WebSocket keepalive timeout, while the camera publishers continued
sending frames. The stationary runtimes were restarted with motion disabled and
new local odometry origins; both rejoined with connection epoch 2. This transport
failure remains part of the evidence record. The subsequent one-minute playback
check sampled the same browser session from 10:36:17 through 10:37:07 UTC and saved
its final screenshot with those statistics. This confirms playback after the
restart; an automatic recovery drill remains pending.

At 11:17 UTC, Ohmni 11 received the reviewed reconnect runtime and its return
controller dependency. It rejoined at epoch 4 with a fresh local odometry origin.
Its camera process remained unchanged and both motion and spotter flags remained
zero. Ohmni 12 was already unreachable and received no update.

A grounded DJI camera check detected no raw AprilTags in 20 frames. The floor
tags were strongly foreshortened in that view. This is a visibility diagnostic;
no hover or flight command was issued. The operator requested a roughly six-foot
hover and declared an 8.5-foot height limit. The new profile must check fresh
flight-controller height continuously while retaining the existing world-navigation
guards for mapped routes. Phone and relay changes remain under review.

The next Ohmni change pairs encoder readings inside the vendor bus owner. It has
not been installed. Lidar orientation and origin measurements also remain
unconfigured, and no physical drive or local collision-stop test has passed.

## Software and remaining evidence

PR numbers link to reviewable source. A software test result does not close a
physical acceptance issue.

| Issue | Software or evidence tooling | Remaining physical evidence |
| --- | --- | --- |
| #78: measured tag grids | Measured worksheet import and validation, [#280](https://github.com/worldofhacks/sweep/pull/280). | Measured black-square sizes, grid spacing, independent zone-to-world ties, and approved placements. |
| #94: shared observations | Bounded frame and observation contract [#260](https://github.com/worldofhacks/sweep/pull/260), MCAP [#261](https://github.com/worldofhacks/sweep/pull/261), authenticated ingress [#263](https://github.com/worldofhacks/sweep/pull/263), localization binding [#264](https://github.com/worldofhacks/sweep/pull/264), and phone producer [#266](https://github.com/worldofhacks/sweep/pull/266). | Measured clock mappings and world registrations for consumers that require them. |
| #247: Ohmni hardware paths | Camera recording [#258](https://github.com/worldofhacks/sweep/pull/258), runtime reliability [#271](https://github.com/worldofhacks/sweep/pull/271), camera ingestion [#278](https://github.com/worldofhacks/sweep/pull/278). | Full hardware inventory, sustained sensor timing, drive/stop qualification, and a measured occupancy map. |
| #246: ground runtime | Authenticated bounded control [#267](https://github.com/worldofhacks/sweep/pull/267), runtime fixes [#271](https://github.com/worldofhacks/sweep/pull/271), bounded approved return [#289](https://github.com/worldofhacks/sweep/pull/289), and transport recovery [#291](https://github.com/worldofhacks/sweep/pull/291). | Continuous odometry, collision and local-stop evidence, connection recovery drills, and physical return-route qualification. |
| #99: survey lifecycle | Audited recording lifecycle [#269](https://github.com/worldofhacks/sweep/pull/269) and console operation [#270](https://github.com/worldofhacks/sweep/pull/270). The survey intent itself emits no velocity. | A supervised mapping recording through the normal ground controls. |
| #81: world bundle | Immutable bundle and registration validation [#259](https://github.com/worldofhacks/sweep/pull/259), with worksheet import [#280](https://github.com/worldofhacks/sweep/pull/280). | Approved measured Level 1 artifacts and retained source evidence. |
| #243: registration and tag fusion | Calibration [#257](https://github.com/worldofhacks/sweep/pull/257), registration [#259](https://github.com/worldofhacks/sweep/pull/259), camera ingestion [#278](https://github.com/worldofhacks/sweep/pull/278), fusion [#279](https://github.com/worldofhacks/sweep/pull/279). | Calibrated cameras, at least three measured tag ties, acceptable registration residuals, and independent tape checks before flight verification. |
| #43, #85, #19, #97: aircraft bring-up and basic control | Existing bridge/control software is retained. Phone observations [#266](https://github.com/worldofhacks/sweep/pull/266) and heartbeat compatibility [#281](https://github.com/worldofhacks/sweep/pull/281) support the live path. | Pinned hardware identity, guarded axis/control tests, measured resend/deadman behavior, positioning-loss handling, and RC takeover. |
| #51, #83: video and calibration | The real DJI feed decodes in the console. Media-path and heartbeat fixes are in [#281](https://github.com/worldofhacks/sweep/pull/281); phone HTTPS media origin support is in [#290](https://github.com/worldofhacks/sweep/pull/290). Existing calibration and latency tools remain applicable. | Twenty to thirty varied calibration frames through the exact pipeline, reprojection error below 0.5 px, and at least 60 seconds of measured glass-to-glass latency. |
| #84: live localization | World-tag consumer [#262](https://github.com/worldofhacks/sweep/pull/262), runtime [#275](https://github.com/worldofhacks/sweep/pull/275), capture alignment [#277](https://github.com/worldofhacks/sweep/pull/277). | Exact aircraft calibration, capture-time alignment, delayed-replay behavior, and localization error against measured checkpoints. |
| #82: flight geometry | Measured geometry and planner-artifact generation [#265](https://github.com/worldofhacks/sweep/pull/265). | Approved corridor/height measurements, swept-clearance checks, and held-out tape checkpoints. A 2D lidar map does not establish overhead clearance. |
| #144, #145: navigation and execution | Signed approval [#272](https://github.com/worldofhacks/sweep/pull/272), route publication [#273](https://github.com/worldofhacks/sweep/pull/273), retained tracking [#274](https://github.com/worldofhacks/sweep/pull/274), phone execution [#276](https://github.com/worldofhacks/sweep/pull/276), fleet capacity [#283](https://github.com/worldofhacks/sweep/pull/283), deployment validation [#284](https://github.com/worldofhacks/sweep/pull/284), frozen inputs [#285](https://github.com/worldofhacks/sweep/pull/285), phone startup admission [#282](https://github.com/worldofhacks/sweep/pull/282), and signed export [#286](https://github.com/worldofhacks/sweep/pull/286). | Accepted map/localization artifacts and physical segment, arrival, cancellation, stale-state, and failure-handling evidence. |
| #86: one-aircraft route acceptance | Existing five-run localization evaluator and its evidence contract are retained. | Five complete approved-route rehearsals, independent checkpoints, failure drills, and signed operator evidence. |
| #20, #18, #87: two-aircraft control and formations | Configured capacity and per-aircraft publication are covered by [#283](https://github.com/worldofhacks/sweep/pull/283). | Each aircraft's individual acceptance, then two-aircraft separation, route, formation, and failure drills. |

The common console UI and live-state fixes are in
[#287](https://github.com/worldofhacks/sweep/pull/287) and
[#288](https://github.com/worldofhacks/sweep/pull/288). The final mixed ground and
phone-navigation composition is in
[#292](https://github.com/worldofhacks/sweep/pull/292), and the reproducible
Ohmni installer is in [#293](https://github.com/worldofhacks/sweep/pull/293).

The map-authoring UI in #248 and destination-intent backend in #143 remain
teammate-owned. Their integration and review are separate dependencies.

## Review order and validation

The draft stack starts from main `cf2f4ead3e0f002932d60972f1f17683dbb40e6a`.
Prerequisite-only branches combine already published source so individual PRs can
show a focused diff. Their PR descriptions list those dependencies; they should
be retargeted as the foundations land. No main-branch merge was performed during
this work.

The camera ingestion, fusion, and worksheet packages passed 22, 52, and 48 tests
respectively. Fleet capacity passed 229 tests, including actual three-aircraft
route publication and a mixed ground/aircraft registry. Deployment validation
passed 26 tests using generated geometry and real localization artifacts; frozen
input handling passed 16 deployment/publication tests. Relay compatibility passed
76 tests, and a heartbeat emitted by the Python relay passed the real Kotlin
phone parser and signature verification.

A fixed integration snapshot at `aedbe6b0` passed all 2,768 Python tests and all
274-file Ruff checks. Later bounded return-file and mixed-selection changes passed
36 affected tests. The explicit simulator-capacity fix passed its startup tests;
a broader run exposed a default-capacity regression, which was corrected and
verified with the four resume-interleaving tests and ten simulator tests.

At the 11:32 UTC check, #271–#286 and #289–#291 plus #293 passed all five CI
jobs. #287 and #288 passed the browser job but failed the build because a helper
excluded Vite's boolean host type. The corrected helper passed the actual build
and its endpoint tests and was pushed to both PRs. #292 inherited that type
failure and two outdated return-test expectations. Its updated prerequisite merge
and helper passed 114 affected Python tests and the console build. CI for those
three updated PRs remains pending at this checkpoint.

Physical captures and private deployment configuration stay with the field
evidence bundle. This document contains no approval key, reader credential, or
generated flight authorization.
