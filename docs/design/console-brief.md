# Sweep operator console: design brief

This is the input to Claude Design. Paste it whole. It asks for a complete, wired, responsive operator console for Sweep, designed as a boutique studio would design it, with every element the PRD requires. Section 10 is the checklist the result is accepted against. Product source of truth: `docs/prd.md`. Wire contract: `console/src/relay/contract.ts` and PRD Appendices A and B.

## 1. What you are designing

Sweep lets one person direct a small fleet of indoor drones from a laptop. The operator clicks a control, reviews the exact request the system is about to send, confirms it, and watches the fleet carry it out on live video. Every request is an Intent v1 envelope. A relay validates it, a deterministic planner turns it into per-aircraft commands, and a safety arbiter checks every intent and every command against limits and live state. The console never decides safety. A physical radio-controller pilot stands beside each aircraft and can pause, take over, return, or land at any time, independent of the network.

The headline workflow today: the operator names a room, selects one hovering aircraft, chooses a capture pattern, clicks Capture room, reviews the plan, confirms, and watches the aircraft collect a panorama or an overlapping photo bundle. Those files become a private AI-generated room world the operator can open from the console. The next workflow, on four aircraft at once: arm, select all, take off, translate together, hold, come home, land all, with a network stop available throughout. Later: a camera wall for four live feeds, detections that ask the operator before anything moves, a room graph and map, spoken and gestured commands into the same request path.

**The operator.** A trained person at a ground station, standing or seated, often glancing between the laptop and the room. Hands may be busy. The room is noisy. Mistakes are expensive, so the console must make the current safety state, the selected aircraft, and any pending request unmistakable at a glance, and must never let a click send something the operator has not seen in full.

**The environment.** A laptop, usually beside a wall of live video. Sometimes a tablet or phone in the operator's hand for a glance at state or to hit stop. Daylight indoor rooms.

## 2. Design direction

Ignore the current console entirely. Do not reference its colors, layout, or components.

- **White base, high contrast, editorial.** Off-white or pure white surfaces, near-black type, hairline rules, a precise grid, generous whitespace. It should feel like a printed instrument manual from a good studio, not a dashboard template.
- **Typography carries the hierarchy.** One text family and one display or numeric family at most, plus a monospace for identifiers and payloads. Large, confident labels for aircraft and states; tabular numerals for every metric.
- **Color means something or is absent.** Ink on paper by default. Reserve saturated color for safety semantics only: stop and emergency, refused and failed, ready and completed, degraded and stale. Never use color as the only carrier of a state; pair it with a word or a mark.
- **Density is deliberate.** The Control module is dense and calm. The Live view is mostly picture. Library and Builder are catalog pages. Connectivity is a table. Configuration is a form. Each module has its own rhythm inside one system.
- **Motion is minimal.** State changes fade or slide briefly. Nothing pulses except a live indicator. Honor reduced-motion preferences.
- **Copy is plain and specific.** Every refusal, failure, and disabled control states its reason in one sentence. No jargon that the tables in section 6 do not define.
- **Formats.** Aircraft identifiers render as `D-01` (two-digit, monospace); stream names as `drone1`. Intent ids shorten to the first eight characters, an ellipsis, and the last four, with the full id on hover and in the JSON block. Times are 24-hour HH:MM:SS local in monospace; ages are "4 s ago"; elapsed is m:ss; calendar dates never appear in copy, only inside the session id. Battery, link, and position quality are whole percentages with tabular numerals.
- **Contrast.** Text at WCAG AAA. Controls and non-text indicators at AA minimum. Test every status color on the white base.

## 3. Information architecture

A single-page application with one persistent shell and six modules. Switching modules never hides safety state and never loses a pending request.

```
Persistent shell (always visible)
├─ Safety bar: network stop · physical RC status · selected aircraft · active intent or plan · link health · warnings
├─ Session strip: relay connection · keyboard-stop connection · session id · roster version · fixture banner
└─ Module navigation
   ├─ Control / Capture
   ├─ Live view
   ├─ Capture library
   ├─ World Builder
   ├─ Connectivity
   └─ Configuration
Later surfaces (design now, ship later): camera mosaic and focus pane · detections and attention · map and room graph · ledger and health · gesture readout · push-to-talk
```

## 4. The persistent shell

Elements, in priority order:

1. **Network stop.** The largest control on every page. It sends `estop`. It is disabled with a stated reason when the console socket is not connected, and it shows "stop active" when the fleet is stopped. Beside it, always: "Physical RC remains primary", the keyboard shortcut Shift+Escape, which travels on a separate authenticated keyboard connection, and the live physical RC status: for each selected aircraft, control authority (Sweep or RC) and RC safety operator present or absent, from `control_authority` and `rc_safety_operator_present`. An RC takeover or an absent safety operator is a state word in the bar, not only a note. While stop is active the control stays enabled and sends `estop` again when pressed; it reads "Stop active" with the time it was raised. There is no console control that clears a stop: it clears only when the relay's state reports `estop: false`, and the shell then shows "Stop cleared" for ten seconds. Never design a resume, reset, or clear-stop control.
2. **Fleet state.** Armed or disarmed, stop active or clear, mode (indoor), roster version, aircraft ready as "n of m".
3. **Selected aircraft.** Identifiers of the current selection, or "none selected".
4. **Active intent or plan.** The one request in flight: name, lifecycle state, short intent id, elapsed time. When a request is pending confirmation, the confirm and cancel actions are reachable from the shell itself. When the relay's pending object carries `expires`, show the remaining time as a countdown beside the elapsed time; at zero the preview is invalidated with the reason "confirmation window expired" and the actions disappear.
5. **Link health.** Relay connection and keyboard-stop connection, each one of connecting, connected, degraded, disconnected. Degraded means the console received a frame it could not parse and dropped it.
6. **Warnings.** A capped list, newest first, each with severity info, warning, or danger and a one-sentence reason.
7. **Fixture banner.** When running on fixture data, a persistent banner says so.

Design the shell for three states: connected and quiet, connected with a pending confirmation, and disconnected.

## 5. Modules

### 5.1 Control / Capture

The working page. Left to right or top to bottom: aircraft registry, controls, plan preview, requests.

**Aircraft registry.** One row or card per aircraft with: identifier, membership state, connection epoch, flight state, battery, link, position quality, control authority, RC safety operator present, readiness reasons (a list of short codes when not ready), advertised capture patterns, last seen, and a select toggle. Selection rules: only ready and selectable aircraft can be selected; at least one aircraft stays selected once any is; a stale selection is cleared visibly when the roster changes. Departed aircraft move to a "departed this session" list with epoch, time, and reason, and return to the registry on rejoin with a new epoch. Empty state for no aircraft. Skeleton rows while connecting.

**Flight controls.** The full Appendix E set, each a named control with its own enabled and disabled reasons: arm, disarm, select all, takeoff (confirm), land (confirm), land all (confirm), hold, translate with direction and step count, altitude up and down by step, formation next and formation set with the five named formations line, column, circle, grid, V, spacing tighter and wider, come home, sweep (confirm), and the network stop. Show which of these the relay currently accepts and which return unsupported at this milestone, without hiding the latter. At M2.0 the relay accepts `arm`, `select`, `takeoff`, `translate`, `hold`, `come_home`, `land_all`, `estop`, and `capture_room`; `disarm`, `land`, `altitude`, `formation_next`, `formation_set`, `spacing`, `sweep`, `survey_area`, and `map_area` are refused as `unsupported`, as is any mode other than `indoor`.

**Capture controls.** Room identifier field with inline validation, capture pattern choice between `pano_360` and `reconstruct_8` with the coverage label each produces (full equirectangular versus incomplete vertical coverage), a readiness panel that names the exact blocking reason, and Capture room. Beside it, the capture-readiness guidance mirror: guidance mode (`visual_advisory` or `registered_metric`), pose source, and pass or fail marks for pose, clearance, camera, storage, motion, and image quality, an azimuth coverage compass with unseen, weak, and accepted sectors, the next heading, and the suggested yaw or gimbal delta. In `visual_advisory` mode the UI must never suggest an XYZ move.

**Plan preview and confirmation.** Appears the moment a draft exists. Shows the plan title, roster version it was built against, ordered steps in plain language, the affected aircraft, and the exact Intent v1 JSON in a block that is expanded every time a preview appears and may be collapsed by the operator afterwards. Two actions: Confirm and send, Cancel. The preview is invalidated visibly when the roster changes, the selection changes, an aircraft leaves, or a configuration change lands; the reason is stated and the actions disappear.

**Requests.** The most recent outcome as a card, then a list of recent requests. Each request shows intent name, lifecycle state, short id, target selection, source, the failed request it retries if any, reason and detail, and a timestamp row for every lifecycle state reached. A failed request offers Retry as new intent, disabled with a reason when the source connection is down or the aircraft is no longer ready.

### 5.2 Live view

The selected aircraft's feed, large. Overlaid or beside it: stream status (live, offline, unreported) with last frame time, health (battery, link, position quality), readiness, guidance mode, and capture progress (ready, capturing, downloading, needs retake, disconnected). Designed to grow into the camera wall: a mosaic of four tiles, later six, each tile with identifier, stream status dot, per-tile battery, link, position, membership, readiness reasons, last frame time, and a Focus action. A focus pane shows the focused tile at size with a center reticle and stream name. Focus follows the operator's selection but survives video loss on the focused aircraft. Degraded states are explicit: no video, stale video, no telemetry, aircraft departed.

### 5.3 Capture library

A catalog of captured media by project, room, capture, aircraft, and time. Each item: thumbnail, pattern, coverage label, file count, checksums, pose metadata (aircraft pose, actual yaw, gimbal pitch, camera intrinsics, timestamp), quality results, and download or export. Filters and a detail view. Empty state for a project with no captures. A "needs retake" flag where quality failed.

### 5.4 World Builder

The room project. A building holds named rooms with explicit doorway adjacency and an optional floor-plan reference. For each room: capture status, an accepted capture bundle selector that lists drone bundles and the manual phone fallback (exactly three overlapping phone photos added from this page, shown as a bundle type visibly distinct from `pano_360` and `reconstruct_8` and usable when the drone path is unavailable), a preview of the exact upload set and model, a Submit action that always shows the `public: false` badge, and the generation job tracked through draft, uploading, queued, running, succeeded, failed, timed out, with retry that preserves the capture. Provenance on every job: operation id, world id, model, timestamps, assets. A succeeded job opens the room world by link or asset preview; the world is labelled "generated", its source photos stay visible beside it, and no copy presents it as a factual or safety record. Both sides of each doorway can be recorded as composition references, kept visibly separate from generation inputs. The operator can keep working on the next room while prior jobs run.

### 5.5 Connectivity

One row per aircraft node and one per shared service. Columns: aircraft, RC controller, Android bridge, LAN, relay, telemetry, camera, video, storage. Cells: status, last seen, round-trip time, stream rate, version, battery, authority, and an actionable error when one exists. Versions include aircraft firmware, controller firmware, phone model, and SDK release. Nothing here is decorative; every cell answers "what is wrong and what do I do".

### 5.6 Configuration

Grouped forms: input device, camera, capture pattern defaults, World API, media, thresholds, connection. Two save semantics, made visible: ordinary settings apply now; safety-sensitive settings are staged and apply between runs, shown as pending until then. Any change while a plan is active invalidates that plan; the form warns before the change and the shell shows the invalidation.

### 5.7 Later surfaces

Design these to the same system now so nothing is bolted on later.

- **Detections and attention.** A detection event carries confidence, class, aircraft, world-position estimate, and time. At or above 0.6 it is shown; at or above 0.8 it promotes its aircraft's feed to focus within one second. The operator marks it real or dismisses it. A detection never emits a command, and the UI says so.
- **Map and room graph.** Aircraft positions on an occupancy map, the room graph with doorways, candidate versus approved capture poses, the geofence, and the batch plan preview for `map_area`: assignments, routes, poses, and patterns frozen into one confirmation.
- **Ledger and health.** The session's accepted, refused, and failed requests over time, replay of a session by id, and health metrics: intent latency, telemetry rate, video latency, unsafe-intent count (always zero), per-aircraft battery and link.
- **Gesture readout.** Camera selection, tracking enabled or disabled (explicit enablement, off by default), a hand-landmark overlay, confidence and dwell feedback, the candidate intent as a preview, confirm and cancel, duplicate suppression indicator, and the enabled gesture-to-intent pairs (first `capture_room`, `hold`, confirm, and cancel) with a note that `estop`, `arm`, `takeoff`, and free-flight motion are not gesture-emittable and stay on the console controls and the physical RC. Distinct states that emit nothing: model failed to load, webcam dropped or unplugged, low confidence, dwell timeout, and duplicate suppressed; each shows the error and that emission is disabled while the network stop and physical RC remain.
- **Push-to-talk.** Press and hold Space (with a guard when typing in a field) or a large button. Recording capped at thirty seconds with a visible countdown. Then the transcript, and a voice outcome card: transcribed or refused, the source, the reason, and the plan preview that follows the same confirm-one-intent-at-a-time rule as every other request. Denied microphone permission, empty audio, upload failure, timeout, and rate limit are distinct states that emit nothing. Two more states emit nothing: ambiguous, where the compiler returns options for the selection or location and the operator picks one or cancels; and language disabled, shown when microphone capture or transcription is unavailable or the LLM API is rate-limited or down with no local fallback, with the reason stated. When the local compiler fallback is in use, the outcome card says so.

## 6. Vocabulary of states

Use these words exactly. The prototype's fixtures must exercise every value.

| Domain | Values |
|---|---|
| Connection | connecting, connected, degraded, disconnected |
| Membership | registered, ready, leaving, disconnected, degraded |
| Membership events | join, readiness, graceful_leave, graceful_leave_completed, unexpected_loss, telemetry_stale, telemetry_recovered |
| Flight state | disarmed, landed, armed, taking_off, airborne, hovering, landing, emergency |
| Intent lifecycle | draft, pending_confirmation, sent, accepted, refused, executing, completed, failed, invalidated, cancelled |
| Stream status | live, offline, unreported |
| Capture pattern and coverage | pano_360 with full_equirectangular; reconstruct_8 with incomplete_vertical_coverage |
| Capture progress | ready, capturing, downloading, needs retake, disconnected |
| Guidance mode | visual_advisory, registered_metric |
| Generation job | draft, uploading, queued, running, succeeded, failed, timed_out |
| Mode | indoor (outdoorC and outdoorF exist in the contract and return unsupported) |
| Readiness reasons | identity_unverified, adapter_capabilities_missing, flight_capability_missing, telemetry_missing, telemetry_stale, home_pose_missing, control_authority_missing, rc_safety_operator_missing, disconnected, leaving |
| Membership reason | authenticated_join, authenticated_rejoin, readiness_gate_failed, graceful_leave_requested, telemetry_recovered, telemetry_stale, graceful_leave_completed, adapter_connection_lost, or null |
| Provenance | adapter_signature, relay_transport_attestation, relay_freshness_attestation, authenticated_adapter_telemetry |

Refusal and failure reasons the UI must render with a plain sentence each:

- **Request shape and routing:** invalid_payload, unknown_source, unknown_intent, unsupported, duplicate_intent, invalid_retry, session_mismatch, source_mismatch, source_not_allowed, frame_not_allowed, downstream_error, downstream_unavailable.
- **Selection and roster:** invalid_selection, stale_selection, stale_roster, stale_connection_epoch, aircraft_not_registered, aircraft_not_ready, invalid_state, confirmation_required, armed_required, active_task.
- **Safety:** estop_active, geofence, ceiling, spacing, battery_reserve, battery_critical, link_quality, link_stale, position_quality, position_stale, operator_absent, control_authority, rc_safety_operator_absent, home_pose_missing.
- **Plan integrity:** invalid_plan, conflicting_motion, invalid_roster_transition, invalid_resume.
- **Camera and media:** storage, camera_unsupported, camera_not_ready, camera_failure, download_failure.
- **Adapter:** adapter_failure, adapter_timeout, planner_failure.

## 7. Wiring

**Connections.** Two WebSockets to the relay at `/ws/{session_id}`: one authenticated as source `console`, one as source `keyboard`. The keyboard connection carries only the Shift+Escape stop. The first frame on each is an auth frame; the token never appears in a URL. There is no automatic reconnect; disconnection is shown honestly. There is also no reconnect control in the console: the disconnected notice reads "Relay disconnected. Reload the console from the operator shell to reconnect. Physical RC remains primary."

**Events the console receives, and what each drives.**

| Event | Fields that matter | Drives |
|---|---|---|
| auth.accepted, auth.refused | source, reason | connection status, warnings |
| state | roster_version, armed, estop, selection, formation, spacing, mode, pending, accepted_plan, drones[], invalidated_intent_ids, invalidation_reason, cleared_control_fields | the shell, the registry, selection, plan invalidation |
| drones[] entries | drone_id, connection_epoch, membership, readiness_reasons, flight_state, battery, link, pos_quality, control_authority, rc_safety_operator_present, last_seen_at, camera_patterns, selectable, adapter_id, adapter_capabilities, membership_history, video.status, video last frame | registry rows, live view tiles, connectivity rows |
| membership | action, drone_id, connection_epoch, membership, readiness_reasons, provenance, reason | registry changes, departed list, warnings |
| telemetry | drone, connection_epoch, x, y, z, vx, vy, vz, battery, state, link, pos_quality | map positions, health metrics; not the registry, which follows state |
| acknowledgement | intent_id, status, command_id, reason, detail, drone_id | request lifecycle, last outcome |
| refusal | intent_id, reason, detail, drone_id | request lifecycle, last outcome, warnings |
| capture_readiness (guidance) | guidance_mode, pose_source, pose_ok, clearance_ok, camera_ok, storage_ok, motion_ok, image_quality_ok, coverage_missing, next_heading_deg, suggested_delta | the readiness compass and gates |
| voice outcome (later) | status, source, reason, transcript, emissions | the voice outcome card |
| detection (later) | confidence, class, drone, position, time | detections list, attention promotion |

**Controls the console sends.** Every control produces an Intent v1 envelope with `intent_id` assigned at draft time and preserved through every state; a retry mints a new id and sets `retry_of`.

| Control | Intent | Args | Confirmation | Selection rule |
|---|---|---|---|---|
| Arm, Disarm | arm, disarm | none | no | any |
| Select | select | ids | no | at least one |
| Takeoff | takeoff | none | yes | selected |
| Land, Land all | land, land_all | none | yes | selected, all |
| Hold | hold | none | no | selected |
| Translate | translate | dx, dy in steps | no | selected |
| Altitude | altitude | delta in steps | no | selected |
| Formation next | formation_next | none | no | selected |
| Formation set | formation_set | name | no | selected |
| Spacing | spacing | delta | no | selected |
| Come home | come_home | none | no | selected |
| Sweep | sweep | optional box | yes | selected |
| Capture room | capture_room | room_id, capture_id, pattern | yes | exactly one |
| Survey area (later) | survey_area | area_id | yes | any |
| Map area (later) | map_area | area_id | yes | non-empty |
| Network stop, Shift+Escape | estop | none | no | fleet |

**Fixtures.** Provide fixture data that matches the field names above for four and for six aircraft, including: one aircraft not ready with reasons, one degraded stream, one departed and rejoined aircraft with a higher epoch, one refused request, one failed request with retry, one invalidated pending plan, one running and one failed generation job, and a disconnected relay.

## 8. Responsive rules

- **Laptop, 1200 px and up.** Full grid. Shell across the top, module navigation at the left, Control as a dense multi-column workspace, Live view as picture with a side rail.
- **Tablet, 880 to 1199 px.** Two columns. Navigation collapses to icons with short labels always visible beneath them; a tap navigates. The mosaic goes to two tiles per row.
- **Phone, below 880 px, verified at 390 px.** Single column. The safety bar is sticky and shrinks to stop, console and keyboard connection status, fleet state, selected aircraft, active request, and the newest danger warning; info and warning severities collapse to a count that opens the list. Navigation becomes a bottom bar. The mosaic becomes a swipeable single tile with the focus pane above it. Confirm and cancel for a pending plan stay reachable without scrolling. Touch targets at least 44 px.
- What never leaves the screen at any width: the network stop, the active request state, and the connection status.
- Wide content such as the connectivity table and the intent JSON scrolls inside its own container. The page never scrolls sideways.

## 9. Accessibility and keyboard

- Every panel labelled; every icon-only control named; every disabled control explains itself in text, not only in a tooltip.
- Shift+Escape is the network stop everywhere. Space is push-to-talk only while focus is on the page body, the workspace landmark, or the push-to-talk button itself; when any other control has focus, Space keeps its native behavior and the push-to-talk button shows "Press Space here or hold this button". Arrow keys move within radio groups and the registry.
- Focus moves to a new plan preview when it appears and returns to the originating control when it resolves.
- Live regions announce request outcomes and info and warning severities politely; danger warnings, stop active, plan invalidation, and the connection warning are assertive alerts. Elapsed-time and countdown counters sit outside every live region.
- Skip link to the workspace. Visible focus rings. Reduced-motion variant. No information carried by color alone.

## 10. Deliverables and acceptance checklist

Deliver, as a working React prototype on the fixtures in section 7 with no runtime dependency beyond React:

1. A token sheet as CSS custom properties: color, type scale, spacing, radius, elevation, motion, and breakpoints.
2. A component set: buttons including the stop variant, status label, metric meter, panel, table, tile, notice, inline confirmation, compass, form controls, empty and skeleton states.
3. The shell and all six modules in their states.
4. The later surfaces in section 5.7 as designed pages.
5. A states gallery page showing every value in section 6 rendered.
6. A responsive demo at 1440, 1024, and 390 px.

Accepted when every line below is present in the prototype:

- [ ] Network stop on every page, disabled with reason when disconnected, showing stop active
- [ ] Physical RC primary note, Shift+Escape hint, and live physical RC status (control authority, RC safety operator present) beside the stop
- [ ] Stop-active state that re-sends on press, clears only from relay state, and no resume or clear-stop control
- [ ] Armed or disarmed, stop active or clear, mode, roster version, aircraft ready n of m
- [ ] Selected aircraft in the shell
- [ ] Active request with lifecycle state and elapsed time in the shell, confirm and cancel reachable
- [ ] Console and keyboard connection status with all four values
- [ ] Warnings list with three severities, capped, newest first
- [ ] Fixture banner
- [ ] Registry row with every field in section 5.1, all five membership states, readiness reasons, epoch
- [ ] Select toggle rules and stale-selection clearing
- [ ] Departed list with rejoin
- [ ] Every Appendix E control with enabled and disabled reasons, and unsupported shown honestly
- [ ] Room field, pattern choice with coverage labels, readiness panel with exact blocking reason, Capture room
- [ ] Capture-readiness compass, gates (pose, clearance, camera, storage, motion, image quality), guidance mode, next heading, yaw or gimbal suggestion, no XYZ in visual_advisory
- [ ] Plan preview with steps, roster version, exact JSON, confirm and cancel, and every invalidation reason
- [ ] Request list with all ten lifecycle states, timestamps per state, retry with new id and retry_of
- [ ] Every refusal and failure reason in section 6 rendered with a sentence
- [ ] Live view single feed with stream status, last frame, health, readiness, guidance mode, capture progress
- [ ] Mosaic of four and of six, focus pane, focus survives video loss, degraded states
- [ ] Capture library with filters, item metadata, checksums, quality, needs-retake flag, export
- [ ] World Builder with building, rooms, adjacency, floor-plan reference, bundle selector including the manual three-photo fallback, upload preview, public false badge, all seven job states, retry, provenance, open world
- [ ] Connectivity table with every column and cell in section 5.5
- [ ] Configuration with apply-now versus staged semantics and plan invalidation warning
- [ ] Detections with the 0.6 and 0.8 thresholds, promotion, mark real or dismiss, never a command
- [ ] Map and room graph with poses, geofence, and the map_area batch confirmation
- [ ] Ledger, replay by session id, health metrics including unsafe-intent count
- [ ] Gesture readout with every element in section 5.7
- [ ] Push-to-talk with countdown, transcript, voice outcome card, and every failure state
- [ ] Three breakpoints verified, nothing safety-critical scrolls away on the phone
- [ ] Contrast, keyboard, focus, live regions, skip link, reduced motion

## 11. Constraints

- React only. Plain CSS with custom properties. No component library, no CSS framework, no dark theme.
- Never render a media URL supplied by an adapter; stream names are derived as `drone{id}`.
- The console never invents state. When the relay says nothing, the UI says unknown or unreported.
- Copy is neutral and specific. No marketing language, no dates, no people's names.
