# Sweep production-chain readiness

At the 15:43 UTC checkpoint on 2026-09-07, neither physical flight nor wheel
acceptance has passed. The second supervised hover reached 1.0 m while the phone
waited four seconds for MSDK control authority without a qualifying confirmation.
Sweep's land command was refused. After the RC landing instruction, telemetry
reported landed at 0.0 m. The network stop was confirmed, but the relay still
reported the session armed after the console's Disarm control became unavailable.

The console now points to session `sweep-field-20260907-v2` through `/field-v2/`,
served on loopback port 18795 by `sweep-supervised-relay-v2`. Its source is the
previous live release plus the reviewed replay-deadline fix. Authenticated state
reports `armed: false` and no selected devices. Public console assets and bootstrap
match the staged artifacts. The previous v1 relay remains on 18794 under `/field/`,
with the older observer on 18793 for rollback.

The phone received APK `c386f4cb`, SHA-256
`88bdd5827734e90fb82fd21617d9ee0d1b898e0d9dab0b8f8e5ef56a4d972e0d`,
verified against the installed package. It includes stationary height polling,
pending SDK-authority handling, and local supervised gimbal pitch controls.
The combined source passed 184 core, 51 bridge-node, and 39 app tests. The phone
joined v2 at epoch 1 with arming off. The grounded gimbal test reached -43.4° for
the requested -45° pitch. Flight control authority and tag visibility remain
unqualified.

The earlier APK `296cc180` height fix passed a 30-second stationary check:
all 30 samples reported 0.0 m with effective ages from -192 to 183 ms, within
the clock-skew and freshness bounds. This supplies height evidence only.

Ohmni 12 is unavailable; the operator returned to Ohmni 11. Before modification,
Ohmni 11's vendor source, metadata, process state, and Sweep inventory were saved.
A private archive preserves 23 source/configuration files, each checked against
its recorded hash. The encoder installer then verified the patched source,
sampler, original backup, permissions, and SELinux context without restarting
the vendor process.

After the operator's normal reboot, the vendor source on disk had reverted to
its original hash. The sampler module remained, but its socket refused the
qualification connection; zero encoder pairs were accepted. A single vendor
native process was present. Sweep runtime and camera publisher remain stopped.
The vendor code provides a plugin directory outside extracted assets; an owner
plugin is being prepared and reviewed. Boot persistence and encoder qualification
are unresolved. Each robot has its
own backup and change record; no wheel command has been issued.

## Observed field behavior

The physical takeoff command at 13:51:23.560 UTC specified `z_mm: 1800` and was
accepted by the phone. Native takeoff reported hovering at 0.90 m, then an enabled
Virtual Stick callback with owner `UNKNOWN` caused `authority_lost`. The safety
hold also failed against that authority latch. The later `land_all` intent was
refused before a device command because control authority was unavailable. The
operator's manual landing completed the physical recovery; Sweep did not complete
the requested takeoff/hold/land sequence. The bounded command record is
`/home/gauntlet/sweep-deploy/evidence/quick-hover-1788789081405/command-evidence.json`.

At 15:33:47 UTC, the second physical attempt reached 1.0 m while the phone waited
4,000 ms for MSDK authority. No qualifying authority confirmation arrived, and the
actual safety hold reported `UNKNOWN`; Sweep refused the network land request.
After the RC landing instruction, telemetry reported landed at 0.0 m at 15:34:27
UTC. The bounded record is
`/home/gauntlet/sweep-deploy/evidence/quick-hover-1788795225951`; it contains
phone logcat and relay telemetry. Its logcat does not record `SessionModel` events,
so it cannot establish a missing DJI callback. The separate stop record at
`/home/gauntlet/sweep-deploy/evidence/quick-hover-1788795291540` and the following
state check confirmed `estop: true` while the session remained armed after the
Disarm control was unavailable. The result does
not qualify takeoff, hold, land, disarm, or authority-loss recovery.

A separate full-history request after the flight failure exposed a relay defect:
replay scanned the roughly 415 MB audit log under its storage lock without checking
the live replay deadline during the scan. Phone heartbeats and reconnects stalled.
The phone recovered at connection epoch 3 before later app updates. The fix in
[#302](https://github.com/worldofhacks/sweep/pull/302) checks the deadline while
reading and validating each record; its main-based head `bc80cf8d` passed 190
audit, rollback, and session tests. It is now deployed in v2. Large-log recovery
remains unqualified, and the older v1 process still has the defect.

At 13:58 UTC, a landed camera check saved 20 D-01 frames at 1280×720, with 18
distinct image hashes. No tag36h11 ID decoded. The floor tags were visible at a
strongly foreshortened angle.

At 15:31–15:32 UTC, the grounded gimbal command reached -43.4° for a -45° request,
within the two-degree tolerance. The 20 frames in
`/home/gauntlet/sweep-deploy/evidence/dji-grounded-tags-20260907T1533-gimbal45`
show a blurred close floor with no readable tags. This confirms pitch actuation only;
it supplies no tag-visibility or localization evidence.

The following table retains earlier field observations.

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

The earlier playback check used console build `7019e68ed759ebc0b49bf89dead224575bd1c28d`. Its
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

At 12:55 UTC, a bounded ADB connection attempt to Ohmni 11 was refused and
`adb devices` was empty. No node configuration changed. The last observation still
belongs to the old session and connection epoch; the node is currently unreachable.
At that checkpoint the new public session had zero devices; installation and
phone handoff were still pending.

An earlier grounded DJI camera check detected no raw AprilTags in 20 frames. The floor
tags were strongly foreshortened in that view. This is a visibility diagnostic;
no hover or flight command was issued. The deployed profile isolates supervised
vertical control from mapped navigation. The replacement aircraft was later identified as DJI Mini 3, firmware
01.00.0500, and passed the stationary height check described above.

An isolated fake-node relay was driven through the production console bundle on a local test server in a real browser.
The browser authenticated, selected and armed D-01, confirmed takeoff, recorded a
relay command of `{"z_mm":1800}`, showed hovering, then confirmed `land_all` and
showed landed. The screenshots are
`/var/tmp/supervised-browser-evidence-20260907-run4/browser-hover.png` and
`/var/tmp/supervised-browser-evidence-20260907-run4/browser-landed.png`. The driver
then timed out while checking for a `Select D-01` button that no longer exists after
selection. The command record and screenshots remain valid browser integration
evidence; the driver run is not reported as a wholly passing harness. It provides
no physical flight evidence.

The owner-side encoder sampler in #298 was installed on Ohmni 11, but the
normal reboot restored the original vendor source and left the sampler inactive.
The vendor source has a fresh backup. At 15:40 UTC, the installer created the
missing plugin directory and installed the owner plugin without changing either
vendor process. Verification recorded the plugin, sampler, and original vendor
source hashes; modes, ownership, and SELinux context matched the reviewed values.
The new plugin directory is mode 0700, and the installed files are mode 0600.
`plugin-install-verification.json` and `CHANGELOG.md` in
`/var/tmp/gauntlet/sweep-production/ohmni11-handback-20260907T150000Z` retain the
record. A normal reboot and a 60-second uninterrupted stationary recording remain
required before ground motion.

The lidar-frame fix in #295 prevents a second rotation of scans that already use
body-relative angles. The operator estimates Ohmni 11's sensor is about 24 inches
above the floor and 22 inches diagonally from the drive-wheel midpoint at 135
degrees counterclockwise from forward. Ohmni 12's sensor is about 21 inches high
and 20 inches diagonally from the midpoint at 120–135 degrees counterclockwise.
Both wheel radii are roughly 3 inches. These rough measurements are not installed
calibration. Both mounts remain unconfigured pending measured offsets and a
stationary target check. A reviewed eight-second probe on Ohmni 11 opened
`ttyUSB0`, returned scan descriptor `a55a0500004081`, then sent STOP, set PWM to
zero, and closed cleanly. Its evidence is
`/var/tmp/gauntlet/sweep-production/ohmni11-handback-20260907T150000Z/lidar-sensor-probe.json`.
The descriptor does not prove scan points or visible mechanical rotation. No physical
drive or local collision-stop test has passed.

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
The normalized lidar frame is in [#295](https://github.com/worldofhacks/sweep/pull/295).
The supervised vertical relay and console readiness policy are in
[#296](https://github.com/worldofhacks/sweep/pull/296), with the phone's continuous
height guard in [#297](https://github.com/worldofhacks/sweep/pull/297). The
owner-side paired encoder sampler and installer are in
[#298](https://github.com/worldofhacks/sweep/pull/298).

The map-authoring UI in #248 and destination-intent backend in #143 remain
teammate-owned. Their integration and review are separate dependencies.

## Review order and validation

The stationary-height polling fix is published as [#299](https://github.com/worldofhacks/sweep/pull/299),
stacked on #297. Its head `1d87e3dce6aecf5c6d813ece33effe8dc417c8f4` passed all five
CI jobs at the 14:13 UTC check; local validation passed 46 bridge-node tests and 39
app tests. The Virtual Stick transition repair is published in [#301](https://github.com/worldofhacks/sweep/pull/301),
authority-only head `faf25bb6ae7667bf26360c2593a7c71c914451ba`. It waits for confirmed SDK
control ownership before starting the supervised climb and handles timeout,
cancellation, and late callbacks. Local source validation passed 184 core, 46
bridge-node, and 39 app tests. The installed APK includes it; hardware verification
remains pending.

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

At the latest CI review, the current heads for the production-readiness stack are
green. This includes the rerun of cancelled current-head workflows, the corrected
capture-hold fixtures in #303, and the JVM authorization-test race fix in #307. Historical
cancelled runs remain in the record; they are not current failures. The fixed
integration snapshot `f517a98f` passed all 2,810 Python tests in 7m48s. The later
`b51c388a` change only makes two Kotlin tests wait for the ready roster before
issuing navigation commands; all 25 RelayLink tests passed. Ruff passed for all
284 Python files.

The console passed 795 tests, lint, and its build. The profile-specific readiness
fixture runs real Python registry state through the TypeScript parser, reducer,
selection chips, and Takeoff control. The real Android build passed 176 core and
45 node tests and produced the published supervised APK from `af0bf300`.

An earlier #297 Python job reached the ten-minute deadline after the
`test_world_bundle.py` progress boundary. An ordered 632-test reproduction passed,
and the subsequent CI run completed in 5m59s. The original stall remains
unexplained in the retained log. An earlier #298 browser run failed a simulated
translation with `adapter_failure`; the next complete browser job passed. These
runs remain in the evidence record.

Physical captures and private deployment configuration stay with the field
evidence bundle. This document contains no approval key, reader credential, or
generated flight authorization.
