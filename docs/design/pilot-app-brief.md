# Sweep pilot app: design brief

This is the input to Claude Design. Paste it whole. It asks for a complete, wired pilot instrument for Sweep's DJI Mini 3 node, designed as a boutique studio would design it, with every element the PRD requires. Section 10 is the checklist the result is accepted against. Product source of truth: `docs/prd.md`. Wire contract: `relay/contracts.py`, `relay/state.py`, `adapters/protocols.py`, the bridge frame plan on issue #43, and PRD Appendices A and B.

## 1. What you are designing

Sweep lets one person direct a small fleet of indoor drones from a laptop. Every request is an Intent v1 envelope; a relay validates it, a deterministic planner turns it into per-aircraft commands, and a safety arbiter checks every intent and every command against limits and live state. The pilot app is the far end of that path: an Android app on the phone clamped to a DJI RC-N1 controller, one phone per Mini 3, four phones in the full fleet. It registers the DJI Mobile SDK, verifies that the connected aircraft is a Mini 3, renders the live feed with capture guidance drawn locally, joins the relay as an authenticated adapter, declares readiness, streams telemetry at 10 Hz, admits signed commands, drives Virtual Stick at a tested rate, runs a watchdog, captures and downloads media, publishes video to the ground station, and records a bench log. It executes only work already issued through the planner and arbiter. It is not a parallel command path, it never decides safety, and it has no stop button of its own: the physical RC in the pilot's hands is the stop.

The headline workflow today: the pilot enters the relay address, session, aircraft number, and node token once; the app registers, confirms the aircraft and controller identity, and joins; the pilot places the aircraft at a clear, central hover point and flips three readiness toggles; the node shows ready on the laptop; the operator confirms `capture_room`; commands arrive; the phone shows the next yaw and gimbal target on a coverage compass; the aircraft captures, the files download with checksums, and one capture bundle goes back. The next workflow: arm, takeoff, translate, hold, come home, and land all through the same command path, with the network stop reaching the node, the watchdog holding on relay loss, and RC takeover proven at every step.

**The pilot.** The RC safety operator. Thumbs on both sticks, phone clamped between them, eyes mostly on the aircraft. They glance at the phone for a second at a time. They can pause, take over, return, or land from the sticks at any moment, independent of the network, and the app must never make that feel like an error.

**The environment.** Landscape, arm's length, bright daylight rooms and dim corners. USB to the controller, LAN to the relay, a guarded empty room, an aircraft hovering at a pose the pilot approved. The aircraft-to-controller feed already spends most of the latency budget, so guidance that matters for piloting is drawn on the phone, not round-tripped through the laptop.

## 2. Design direction

Ignore the DJI sample app's look and the Sweep console's layout. Do not reference either's colors or components.

- **A pilot instrument over video.** The flight display is full-bleed FPV with translucent dark scrims at the top and bottom edges, white type, and one accent. Large type, short words, whole numbers. Every control a thumb needs sits at the left or right edge within reach from the sticks; the center band over the picture carries no interactive element while flying.
- **Paper for everything else.** Setup, Readiness, Commands, Capture, Connectivity, Capabilities, and Bench use a white base with near-black type, hairline rules, and a precise grid, so they read as the console's family. One system, two grounds.
- **Typography carries the hierarchy.** One text family plus a monospace for identifiers, checksums, and payloads. Tabular numerals for every metric. Guidance words on the overlay sized to be read at arm's length in one glance.
- **Color means something or is absent.** Ink on paper, white on scrim by default. Saturated color only for safety semantics: stop and failsafe, refused and failed, ready and completed, degraded and stale. Never color alone; pair it with a word or a mark. Compass sectors are told apart by mark first, color second.
- **No dark decorative theme, no gamer aesthetic.** The scrims exist for contrast over an unpredictable picture, not for mood. No glows, no chrome, no neon, no faux instrument bezels.
- **Motion is minimal.** The reticle and compass follow yaw without easing lag. State changes fade briefly. Nothing pulses except a live indicator. Honor reduced-motion preferences.
- **Copy is plain and specific.** Every refusal, failure, hold, and disabled control states its reason in one sentence, using the words in section 6.
- **Formats.** Aircraft identifiers render as `D-01` (two-digit, monospace); stream paths as `drone1`. Command, intent, event, and capture identifiers shorten to the first eight characters, an ellipsis, and the last four, with the full value on tap. Checksums show the first eight hex characters with the full value on tap. Ages are "4 s ago"; durations are m:ss; rates are "10.0 Hz"; latencies are whole milliseconds; headings are whole degrees; deltas carry a sign; battery, link, and position quality are whole percentages; temperatures are whole degrees with the thermal status word beside them. Calendar dates never appear in copy, only inside the session id.
- **Contrast.** Overlay text at WCAG AAA against the scrim, verified against a white-wall frame and a dark-corner frame. Controls and marks at AA minimum. Text on the white base at AAA.

## 3. Information architecture

One activity, one foreground service, eight screens. The flight display is home and is always one tap away. Switching screens never hides authority, watchdog, or connection state, and never interrupts the service.

```
Persistent strip (every screen; on scrim over video, on white elsewhere)
├─ Network stop indicator · control authority and last change reason · RC safety operator · watchdog
├─ Relay connection · membership and epoch · flight state · battery · link · fake-SDK banner
└─ "Physical RC remains primary" · Return to flight
Screens
   ├─ Setup
   ├─ Flight display (home)
   ├─ Readiness
   ├─ Commands
   ├─ Capture
   ├─ Connectivity
   ├─ Capabilities
   └─ Bench
Later surface (design now, ship later): registered_metric panel with XYZ delta, pose age, and uncertainty
```

## 4. The persistent strip

Elements, in priority order:

1. **Network stop indicator.** Driven only by relay state `estop`. When true it reads "Network stop active" and says what the node is doing: neutral sticks and hover, then land if the stop is held. It clears only when relay state reports `estop: false`, then reads "Stop cleared" for ten seconds. There is no stop, resume, or clear control in the app; the physical RC is the stop, and the strip says so. Never design one.
2. **Control authority.** "Sweep" or "RC", from the node's own `control_authority`, with the SDK's last authority change reason rendered verbatim and a plain sentence beside it. Beside that, RC safety operator present or absent. Authority lost is a state word here, not only a toast.
3. **Watchdog.** fresh, hold, or failsafe, with the age of the last authenticated relay activity and the relay-distributed hold and failsafe thresholds. Hold and failsafe are assertive states with a sentence each.
4. **Relay connection.** connecting, connected, degraded, or disconnected, then membership state and connection epoch. Degraded means a frame arrived that could not be parsed and was dropped. Unlike the console, the node reconnects with backoff; the strip shows the next attempt as a countdown.
5. **Flight state, battery, link.** From the SDK, exactly as the node reports them in telemetry.
6. **Physical RC primary note.** Always present: "Physical RC remains primary".
7. **Fake-SDK banner.** When running the fake flavor with no aircraft, a persistent banner says so.

Design the strip for four states: connected and quiet, network stop active, authority lost, and relay disconnected with the watchdog in hold.

## 5. Screens

### 5.1 Setup

The first-run and diagnostics page, white base.

**Relay fields.** Relay URL, session id, and aircraft number (1 to 4, rendered as `D-01`), each with inline validation. Node token entered once, masked, stored on the device, never shown again in full, with a Replace token action. The token never appears in a URL. Save and Connect actions, each with its disabled reason.

**Package and key.** Application id, build flavor (fake or probe), and whether the SDK key is present for that package. A missing key is a hard stop with a sentence, not a crash.

**SDK registration.** unregistered, registering, registered, registered_offline (from the cached result with no network), or failed with the SDK error rendered verbatim. First-run registration is rehearsed here before any flight session.

**Identity.** Product type, which must be the Mini 3; a mismatch reads "Wrong aircraft" and blocks flight. Aircraft firmware, RC firmware, phone model, Android version, build number, and SDK version, each compared to the pinned hardware profile: "matches" or "differs" with the field named.

**Self-check list.** One row per gate with pass, fail, or pending and a sentence: USB accessory attached, SDK registered, aircraft connected, RC connected, identity verified, key update rates measured, camera probe complete, clock offset measured from relay frames, relay reachable, MediaMTX reachable, aircraft storage writable, phone battery above the bench threshold, thermal status none. Go to flight is enabled when the flight-critical rows pass and warns about the rest.

States: first run empty, connecting, registered and verified, registered offline, wrong aircraft, key missing, registration failed, profile differs.

### 5.2 Flight display

Full-bleed FPV from the SDK surface. Everything else sits on the top and bottom scrims or at the two thumb edges.

**Top scrim.** The persistent strip, plus gimbal pitch, aircraft storage remaining, camera state (ready, busy, error, unsupported), virtual stick enabled or disabled with the stick send rate, video publish state, phone battery, and phone thermal status. Position quality carries a "provisional" mark until the indoor mapping is measured.

**Center.** A reticle. Around it, the azimuth coverage compass: the measured horizontal field of view divides the ring into sectors, each marked unseen (hollow), weak (hatched), or accepted (filled), with color as the second channel. A marker shows `next_heading_deg`; at the reticle an arrow reads the `suggested_delta` as "yaw +12°" or "gimbal −15°". Arrows mean yaw or gimbal only. In `visual_advisory` no element ever suggests a left, right, forward, back, up, or down move, and clearance reads "pilot approved".

**Bottom scrim.** The capture state pill: Ready, Capturing, Downloading, Needs retake, Disconnected. Beside it the guidance mode (`visual_advisory` or `registered_metric`) and pose source (`operator_approved`) labels, large. Then the quality checks as a row of marks with a word each: blur, exposure, feature overlap, motion, link, battery, storage, camera readiness, and the readiness gates from `capture_readiness`: pose, clearance, camera, storage, motion, image quality. A failing check names itself in one sentence. Capturing shows panorama progress as a percentage or reconstruct progress as "3 of 8"; Downloading shows file n of m.

**Edges.** Left edge: the screen rail. Right edge: Local cancel, the largest control on the display. Local cancel stops the node's own stick output, holds, reports `authority_lost` with the change reason "pilot cancel", fails the active command with `authority_lost`, and turns the Control authority toggle off; the pilot re-grants authority on the Readiness screen. Cancel during a capture preserves the partial bundle as failed evidence.

**Degraded states, each explicit.** No video with the last frame age; stale video; no telemetry; aircraft disconnected; RC disconnected; relay disconnected with the watchdog state; network stop active; watchdog hold; watchdog failsafe (land indoors, never return home); authority lost with the reason. Design the display in Ready, Capturing, Downloading, Needs retake, Disconnected, authority lost, watchdog hold, and network stop active.

**Later surface.** A `registered_metric` panel with an XYZ delta, pose age, and uncertainty, hidden until that mode is active, drawn now so nothing is bolted on.

### 5.3 Readiness

White base. Three toggles, each sending a signed readiness frame with the current `connection_epoch`, each with its consequence in a sentence:

- **Home pose confirmed.** On: the relay records the current telemetry position as home and `come_home` targets it; this requires current telemetry, otherwise the relay keeps `home_pose_missing`. Off: the relay clears home.
- **Control authority.** On: Sweep may drive Virtual Stick. Off: the node fails motion commands with `authority_lost` and the relay reports `control_authority_missing`.
- **RC safety operator present.** Off: the relay reports `rc_safety_operator_missing` and the arbiter refuses motion.

Below, the relay's answer: membership state, `readiness_reasons` as a list with a sentence each, `reason` (`readiness_gate_failed` or none), and provenance. Beside it, what the relay checks: identity verified, capabilities including `flight`, telemetry fresh with its age against the freshness limit. A readiness frame sent while disconnected or leaving is refused with `invalid_membership_transition`; show that. Leave session sends `graceful_leave`, enabled only when landed, disarmed, and task-free, refused otherwise with `graceful_leave_not_authorized`; leaving clears the laptop's selection and pending plans, and the screen says so. Rejoin after a leave or a relay restart increments the epoch, shown here.

States: not yet joined, degraded with reasons, ready, leaving, disconnected, refused toggle.

### 5.4 Commands

White base. **Current command** as a card: operation, short command id and intent id, `seq`, `roster_version`, `connection_epoch`, args, issued age, TTL remaining as a countdown, signature verified mark, and lifecycle state with a timestamp row for every state reached: received, accepted, executing, completed or failed with reason and detail. Long-running operations (`capture_panorama`, `retrieve_media`) show in-progress with a percentage.

**Admission outcomes.** A frame failing signature verification is dropped and logged locally, never acknowledged. `stale_command` for an expired TTL, a stale roster version, or a stale epoch; `out_of_order_command` for a non-monotonic `seq`; `authority_lost`; `watchdog_hold`; `watchdog_failsafe`. Each renders with a sentence.

**Command log.** Newest first, every command this epoch with its outcome, filterable by outcome. **Watchdog card.** State, age of last activity, both thresholds, and what happens: at hold, neutral sticks and hover and `watchdog_hold`; at failsafe, the configured failsafe (land indoors), Virtual Stick disabled, `watchdog_failsafe`. Relay socket loss starts the same clock. **Stick output.** Enabled or disabled, mode, send rate against the relay-distributed rate, last frame age.

States: idle, executing with countdown, completed, failed with each reason, hold, failsafe, network stop active.

### 5.5 Capture

White base. **Current capture.** `room_id`, `capture_id`, pattern (`pano_360` or `reconstruct_8`), coverage label (`full_equirectangular` or `incomplete_vertical_coverage`), status (completed, unsupported, failed) with reason and detail. Changing patterns happens on the laptop with a new preview; the phone shows which is active.

**Progress.** For `reconstruct_8`, a step list per heading: rotate, settle, gimbal, camera ready, capture, file created, with "n of 8". For `pano_360`, the panorama progress percentage and the verification result: a 2:1 equirectangular image with full vertical coverage, or not, in which case the pattern returns `unsupported`.

**Files.** One row per `media_file`: short file id, thumbnail, actual yaw, gimbal pitch, timestamp, size, checksum (`sha256`, short with full on tap), intrinsics, retrieval status (completed, failed, unsupported), and download progress while retrieving. **Bundle.** File count, coverage label, status, and the missing sectors when Needs retake. **Retake.** The phone cannot issue `capture_room`; Request retake publishes the missing coverage in `capture_readiness` with the sentence "The operator re-issues capture from the laptop".

States: no capture, capturing, downloading, complete, needs retake with sectors, unsupported pattern, failed with partial evidence preserved.

### 5.6 Connectivity

White base, one table. Rows: aircraft, RC, USB, LAN, relay, MediaMTX publish, telemetry, storage. Columns: status, last seen, round-trip time, rate, version, error. Cells: aircraft connection, firmware, and link signal quality; RC connection, firmware, RC battery, and authority; USB accessory attached and since; LAN interface, address, and gateway RTT; relay socket state, auth state, session id, epoch, clock offset, heartbeat RTT, frames in and out per second, last frame age, and the last refusal reason with a sentence; publish state, path `drone1`, codec and profile, resolution, frame rate, keyframe cadence, bitrate, dropped frames, and last error; telemetry send rate and each SDK key's measured rate; aircraft storage and phone storage remaining. Actions: Reconnect relay, Restart publish, Copy diagnostics. Nothing here is decorative; every cell answers "what is wrong and what do I do".

States: all healthy, relay disconnected, aircraft disconnected with the socket held up, publish failed, refusal shown.

### 5.7 Capabilities

White base. **Runtime probe.** The camera mode range and panorama modes the SDK returns, normalized into `native_panorama_modes`; `photo_capture`; `gimbal_pitch_min_deg` and `gimbal_pitch_max_deg`; `horizontal_fov_deg` as measured, with the note that the published lens value is not a horizontal field of view; `storage_remaining_bytes`; `media_retrieval`; stream info (mime type, width, height, frame rate, keyframe cadence, profile and level); and each listened key with its measured update rate. **Pattern verdicts.** `pano_360` supported, unsupported, or unverified until the hardware test passes; `reconstruct_8` supported or unsupported. **Hardware profile.** Aircraft model, aircraft firmware, RC firmware, phone model, Android version, build number, SDK version, and measured horizontal field of view, compared to the pinned profile. Actions: Probe again, and the sent `capabilities` frame as a JSON block, collapsed.

States: not probed, probing, probed, differs from profile, panorama unsupported.

### 5.8 Bench

White base. Start and Stop the 15-minute recording with elapsed and remaining as m:ss. **Live metrics** as tabular numerals: command RTT, jitter, drops, stick send rate, acknowledgement latency, telemetry rate, video latency by leg (aircraft to controller, Android processing, LAN delivery) and glass-to-glass p95 against the 300 ms target, decoded frame rate and decode drops, dropped published frames, phone temperature, thermal status, throttling, battery draw, and the spread between nodes when the relay fans one plan to several. **Export** hands the JSONL and the report to the share sheet and marks the run exported.

States: idle, recording, stopped early with the reason, complete, exported.

## 6. Vocabulary of states

Use these words exactly. Values marked proposed are not yet in a frozen contract; the fixtures use them until the contract PR confirms them.

| Domain | Values |
|---|---|
| Relay connection | connecting, connected, degraded, disconnected |
| Membership | registered, ready, leaving, disconnected, degraded |
| Membership events | join, readiness, graceful_leave, graceful_leave_completed, unexpected_loss, telemetry_stale, telemetry_recovered |
| Membership reason | authenticated_join, authenticated_rejoin, readiness_gate_failed, graceful_leave_requested, telemetry_recovered, telemetry_stale, graceful_leave_completed, adapter_connection_lost, or null |
| Readiness reasons | identity_unverified, adapter_capabilities_missing, flight_capability_missing, telemetry_missing, telemetry_stale, home_pose_missing, control_authority_missing, rc_safety_operator_missing, disconnected, leaving |
| Provenance | adapter_signature, relay_transport_attestation, relay_freshness_attestation, authenticated_adapter_telemetry |
| Flight state | disarmed, landed, armed, taking_off, airborne, hovering, landing, emergency |
| Operations | takeoff, goto, rotate_to, hover, land, estop, camera_capabilities, set_gimbal_pitch, camera_ready, capture_panorama, capture_photo, retrieve_media |
| Acknowledgement status | accepted, executing, completed, failed (refused and invalidated exist in the lifecycle and are relay-side) |
| Node admission outcomes | stale_command, out_of_order_command, authority_lost, watchdog_hold, watchdog_failsafe |
| Watchdog | fresh (proposed), hold, failsafe |
| Loss behavior | hold, failsafe |
| Control authority | Sweep, RC; change reason rendered verbatim from the SDK |
| Camera state | ready, busy, error, unsupported |
| Capture result | completed, unsupported, failed |
| Capture pattern and coverage | pano_360 with full_equirectangular; reconstruct_8 with incomplete_vertical_coverage |
| Capture progress | Ready, Capturing, Downloading, Needs retake, Disconnected |
| Guidance mode and pose source | visual_advisory, registered_metric; operator_approved |
| Suggested delta kind | yaw, gimbal |
| Coverage sector | unseen, weak, accepted |
| Quality checks | blur, exposure, feature overlap, motion, link, battery, storage, camera readiness |
| SDK registration | unregistered, registering, registered, registered_offline, failed (proposed) |
| Video publish | idle, connecting, publishing, degraded, failed (proposed) |
| Phone thermal status | none, light, moderate, severe, critical, emergency, shutdown |
| Auth | auth.accepted; auth.refused with invalid_auth, unknown_source, authentication_failed |

Relay refusals of node frames the UI must render with a plain sentence each: frame_not_allowed, source_not_allowed, session_mismatch, drone_identity_mismatch, invalid_signature, stale_timestamp, future_timestamp, out_of_order_event, replayed_event, unknown_aircraft, stale_connection_epoch, invalid_membership, invalid_membership_transition, invalid_telemetry, out_of_order_telemetry, invalid_acknowledgement, graceful_leave_not_authorized, fleet_capacity.

Reasons the node attaches to a failed acknowledgement, a capture result, or a media result, each with a sentence: unsupported, storage, camera_unsupported, camera_not_ready, camera_failure, download_failure, adapter_failure, adapter_timeout, plus the five admission outcomes above.

## 7. Wiring

**Connections.** USB accessory to the RC-N1 through the SDK; attaching the cable launches the app. One WebSocket from the foreground service to the relay at `/ws/{session_id}`; the first frame is `auth` with source `adapter`, the aircraft number, and the token; the token never appears in a URL. The relay answers `auth.accepted` carrying the command TTL, the stick rate, and the watchdog hold and failsafe thresholds, so every threshold on screen is relay-distributed, never a local default. The node reconnects with backoff and increments its connection epoch on rejoin; an aircraft or RC disconnect keeps the socket up and sends readiness with authority false. Video publishes over RTSP to the ground-station path `drone{id}`.

**SDK sources and what each drives.**

| SDK source | UI element |
|---|---|
| KeyProductType, KeyRcFirmwareInfo, aircraft firmware | Setup identity, hardware profile |
| KeyConnection for aircraft and RC | strip, Connectivity rows, degraded states |
| KeyAircraftLocation3D, KeyAircraftVelocity, KeyAircraftAttitude, KeyAltitude, KeyUltrasonicHeight | telemetry x, y, z, vx, vy, vz; compass yaw; motion check; key rates |
| KeyFlightMode, KeyAreMotorsOn, KeyIsFlying | flight state |
| KeyChargeRemainingInPercent, KeySignalQuality | battery, link |
| Virtual Stick state, FlightControlAuthorityChangeReason | virtual stick enabled, authority, change reason |
| KeyCameraModeRange, panorama modes, KeyPhotoPanoramaProgress | Capabilities probe, capture progress |
| Gimbal attitude | gimbal pitch |
| Camera storage, camera state | storage, camera state |
| Camera stream surface, StreamInfo, SPS profile and level | FPV, stream info, decode metrics |
| MediaDataCenter list and download | file rows, download progress |
| Android battery and thermal status | phone battery, thermal |

**Frames the node receives.**

| Frame | Fields that matter | Drives |
|---|---|---|
| auth.accepted, auth.refused | reason, ttl_ms, stick rate, hold_ms, failsafe_ms | connection state, thresholds |
| command | command_id, intent_id, roster_version, connection_epoch, seq, issued_at, ttl_ms, operation, args, signature | current command, admission, log |
| state | estop, armed, selection, roster_version, pending, drones[] | stop indicator, roster check |
| refusal | reason, detail | Connectivity last refusal, Readiness |

**Frames the node sends.**

| Frame | Fields | Shown at |
|---|---|---|
| membership join | adapter_id, capabilities | Readiness |
| membership readiness | connection_epoch, home_pose_confirmed, control_authority, rc_safety_operator_present | Readiness toggles |
| membership graceful_leave | connection_epoch | Readiness |
| telemetry | drone, connection_epoch, x, y, z, vx, vy, vz, battery, state, link, pos_quality | strip, Connectivity rate |
| acknowledgement | intent_id, command_id, status, reason, detail, connection_epoch, roster_version | Commands |
| capabilities | the camera capability fields plus the hardware profile | Capabilities |
| capture_readiness | guidance_mode, pose_source, pose_ok, clearance_ok, camera_ok, storage_ok, motion_ok, image_quality_ok, coverage_missing, next_heading_deg, suggested_delta | overlay |
| node_status | virtual stick enabled, control authority and change reason, watchdog state, video publish state, phone battery, thermal | strip, Connectivity |
| media_file, capture_bundle | the fields in section 5.5 | Capture |

**Fixtures.** Provide fixture data shaped like these frames for: a fresh install; a registered-offline start; a wrong aircraft; a node degraded with three readiness reasons; a ready node; a capture of each pattern with progress, one file failing retrieval, and a Needs retake with two missing sectors; a `pano_360` returning unsupported; every admission outcome; a watchdog hold and a failsafe; an RC takeover mid-translate; network stop active; relay disconnected with reconnect attempts; a rejoin with epoch 2; a publish failure; a thermal status of severe during the bench.

## 8. Device rules

- **Landscape only, phone clamped.** Primary canvas 2400 by 1080; check at 1600 by 720. The video surface bleeds under the display cutout; overlays avoid the cutout and system-bar insets. Immersive mode, screen kept on, brightness untouched.
- **Thumb zones.** Controls within reach of a thumb resting on a stick: the outer fifth of the width on each side and the bottom scrim. Local cancel at the right edge, the screen rail at the left. Every touch target at least 48 dp with 8 dp between targets. Nothing interactive in the center band while a command executes.
- **Scrims.** Top and bottom only, tall enough for one row of large type, never a full-screen dim. Overlay text keeps AAA over both a white wall and a dark corner.
- **Non-video screens.** One column with a maximum content width; wide content such as the Connectivity table and JSON blocks scrolls inside its own container. The page never scrolls sideways.
- What never leaves the screen: the network stop indicator, control authority, watchdog state, relay connection, and Return to flight.

## 9. Accessibility

- Every control has a content description; every icon-only control is named; every disabled control explains itself in text, not only in a tooltip.
- Compose live regions: capture progress, file downloads, and readiness changes announce politely; authority lost, watchdog hold and failsafe, network stop active, and relay disconnected are assertive. Countdowns and ages sit outside every live region.
- Haptics for the assertive states and for capture complete, because the pilot's eyes are on the aircraft. Optional short tones, off by default.
- Font scaling to 130 percent without clipping the overlay. Visible focus for external keyboards. Reduced-motion variant. No information carried by color alone.

## 10. Deliverables and acceptance checklist

Deliver, as a clickable Compose prototype on the fixtures in section 7 with no dependency beyond Jetpack Compose and Material 3:

1. A token sheet as Compose-ready values: color, type scale, spacing, radius, elevation, motion, scrim opacity, and the two grounds.
2. A component set: strip, status label, metric meter, reticle, compass, delta arrow, capture pill, quality check row, toggle with consequence, command card, table, notice, JSON block, empty and skeleton states.
3. All eight screens in their states.
4. The later `registered_metric` panel as a designed component.
5. A states gallery screen showing every value in section 6 rendered.
6. A landscape demo at 2400 by 1080 and a check at 1600 by 720.

Accepted when every line below is present in the prototype:

- [ ] Strip on every screen with stop indicator, authority and change reason, RC safety operator, watchdog, relay connection, membership and epoch, flight state, battery, link
- [ ] No stop, resume, or clear-stop control anywhere; the stop indicator driven by relay state only
- [ ] "Physical RC remains primary" and Return to flight on every screen
- [ ] Setup with relay fields, token entered once and masked, key status, all five registration states, identity with the wrong-aircraft stop, profile comparison, self-check list
- [ ] Flight display with FPV, every top-scrim metric, reticle, compass with three sector marks, next heading, yaw or gimbal delta, no XYZ in visual_advisory, clearance reads pilot approved
- [ ] All five capture states, guidance mode and pose source labels, every quality check and readiness gate
- [ ] Local cancel at the right edge with its consequences
- [ ] Every degraded state in section 5.2
- [ ] Readiness with three toggles and consequences, the relay's answer with reasons and provenance, refused toggle, leave session with preconditions, rejoin with a new epoch
- [ ] Commands with the current card, TTL countdown, every lifecycle state, every admission outcome, the dropped-signature entry, log, watchdog card, stick output
- [ ] Capture with pattern, coverage label, per-pattern progress, file rows with checksums and retrieval status, bundle, retake request
- [ ] Connectivity table with every row, column, and cell in section 5.6 and its three actions
- [ ] Capabilities with the probe, pattern verdicts, hardware profile comparison, sent frame
- [ ] Bench with start and stop, every metric, export
- [ ] Every value in section 6 rendered in the gallery, every refusal and reason with a sentence
- [ ] Both canvases verified, thumb zones respected, 48 dp targets, nothing safety-critical scrolls away
- [ ] Contrast over video, live regions, haptics, font scaling, reduced motion

## 11. Constraints

- Jetpack Compose and Material 3 only. No third-party UI kit, no dark decorative theme; the scrim is a contrast device, not a theme.
- The app never issues an intent, never emits a command of its own, and never clears a stop. It executes signed commands and reports.
- The app never invents state. When the SDK or the relay says nothing, the UI says unknown or unreported.
- Copy is neutral and specific. No marketing language, no dates, no people's names.
