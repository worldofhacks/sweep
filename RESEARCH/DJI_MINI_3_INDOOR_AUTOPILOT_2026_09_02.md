# DJI Mini 3 telemetry and indoor-autopilot boundary

Date: 2026-09-02

## Recommendation

Use the Mini 3 as a DJI control, video, and room-capture platform. Do not treat it as a bundled indoor-autonomy platform.

For the M1 vertical slice, DJI's flight controller keeps the aircraft stable while a safety pilot places it at an approved capture pose. Sweep then owns the confirmed capture sequence: hold, yaw and gimbal steps, camera readiness, capture, media retrieval, and Marble submission. This phase does not need autonomous room traversal.

For autonomous motion, add an external shared indoor pose source and independent collision-clearance observations. The Mini 3 has downward vision and infrared sensing for local hover, but no forward, rear, side, or upward obstacle sensors. DJI documents its precise-hover range as 0.5 to 10 m over patterned, diffusely reflective surfaces with more than 15 lux. DJI's Virtual Stick API documents obstacle-avoidance support for other aircraft families, not Mini 3. [Mini 3 specifications](https://www.dji.com/mini-3/specs) · [Virtual Stick API](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html)

## What DJI supplies

The aircraft firmware supplies the inner flight-control loops, stabilization, battery management, radio link, camera and gimbal control, automatic takeoff and landing, local hover, and configured link-loss behavior. MSDK supplies Android libraries, samples, a simulator, keyed state access, live video and media access, device-health information, flight logs, waypoint and Virtual Stick interfaces. DJI describes Virtual Stick as the real-time route for developer-provided automation and recommends sending it at 5 to 25 Hz. [MSDK introduction](https://developer.dji.com/doc/mobile-sdk-tutorial/en/basic-introduction/msdk-introduction.html) · [MSDK architecture](https://developer.dji.com/doc/mobile-sdk-tutorial/en/basic-introduction/overview.html) · [Virtual Stick tutorial](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/virtual-stick.html)

These are platform-level interfaces. The official API pages do not guarantee that every key, mission type, or update rate works on Mini 3. The exact aircraft, RC-N1, Android model, firmware, and MSDK release must be probed and recorded before Sweep freezes its telemetry contract.

## Telemetry to probe on the real Mini 3

| Group | Documented MSDK values | Sweep use | Important limit |
|---|---|---|---|
| Flight state | connection, motors on, flying, flight mode, failsafe state | arming, state machine, refusal and recovery | Product support and listener rate require measurement. |
| Fused motion | latitude, longitude, altitude; pitch, roll, yaw; NED velocity | display, health checks, controller feedback | The position is geographic, not a documented shared indoor Cartesian pose. GNSS-denied horizontal position cannot be assumed valid. |
| Downward ranging | `KeyUltrasonicHeight`, which may fuse barometer, downward vision, and infrared ranging | altitude and floor-clearance observation | It is a fused height, not a raw depth image or directional obstacle map. Mini 3 runtime support must be verified. |
| GNSS and return | home-set state, home coordinate, flight mode, go-home state, GNSS-related status | outdoor health and recovery | DJI says starting intelligent RTH requires good GPS. It is not a dependable indoor return planner. |
| Battery | remaining mAh and percent, voltage, current, temperature, cell voltages, connection | reserve, return and land thresholds | Freeze the exact keys and measured rates supported by Mini 3. |
| Radio | aircraft and RC connection, airlink signal quality, working band and interference information where supported | degraded-link warnings and watchdog input | Signal quality is not position or obstacle evidence. |
| RC and authority | RC connection and battery, flight mode, Virtual Stick authority/state | safe handoff and takeover | Network stop is not an independent e-stop; the physical RC remains primary. |
| Camera and gimbal | connection, mode, storage, capture state, gimbal attitude, live stream, media list and download | room capture and provenance | Generic panorama keys do not prove that Mini 3 returns a full equirectangular artifact. |
| Health | device-health messages, compass and IMU calibration/status, flight logs | preflight gate and diagnosis | MSDK exposes processed health/status. I found no published Mini 3 MSDK interface for synchronized raw accelerometer, gyroscope, downward-camera, or depth data. |

The flight-controller API documents geographic location, attitude, NED velocity, flight mode, failsafe state, home state, and fused ultrasonic height. The battery API documents remaining capacity, percentage, temperature, voltage, current, and cell voltages. The AirLink API documents connection and signal quality. [Flight-controller keys](https://developer.dji.com/api-reference-v5/Components/IKeyManager/Key_FlightController_FlightControllerKey.html) · [Battery keys](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Battery_BatteryKey.html) · [AirLink keys](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Airlink_AirlinkKey.html)

## What Sweep must build

1. **DJI Android bridge.** Register MSDK, connect to the RC-N1, probe capabilities, normalize key updates, timestamp and sequence messages, relay video and media, send Virtual Stick commands, reject stale or out-of-order commands, and stop network control on heartbeat loss.
2. **Map-frame localization adapter.** Transform an external tracker into one shared metric frame for every aircraft. Report pose, covariance or quality, update age, source identity, and calibration version. Fail closed when the source is stale, discontinuous, or inconsistent with flight-controller motion.
3. **Collision-clearance adapter.** Supply independent observations for forward, rear, lateral, upward, and downward clearance. The arbiter must refuse motion when a protected direction is missing or stale.
4. **Known-map planner.** Load a surveyed occupancy map and room graph, inflate obstacles by the aircraft and guard radius, find routes through approved doorways, smooth them into bounded velocities, and revalidate every segment before dispatch.
5. **Trajectory tracker.** Convert map-frame position error into bounded velocity and yaw commands at a measured rate inside DJI's 5-to-25 Hz recommendation. Include acceleration, speed, stopping-distance, command-age, and tracking-error limits.
6. **Swarm coordinator.** Assign rooms, reserve doorway and corridor volumes, maintain separation, and serialize narrow passages. A single-aircraft route must pass before two- and three-aircraft trials.
7. **Recovery state machine.** Define behavior for laptop-LAN loss, Android-RC loss, aircraft link loss, positioning loss, clearance-sensor loss, low battery, pilot takeover, and partial capture. Each state needs a tested hold, land, or physical-RC handoff.
8. **Evidence recorder.** Store bridge versions, capabilities, telemetry age and rate, commands and acknowledgements, localization quality, clearance observations, plan revisions, capture metadata, and failure transitions.

## Indoor localization choices

For a bounded M1/M2 lab, an external motion-capture system or calibrated overhead cameras tracking a marker on each aircraft is the cleanest route because it produces one common frame without modifying DJI firmware. This still requires coverage through every doorway and room, calibration, occlusion handling, and a stale-pose safety path.

UWB anchors and a lightweight tag are another candidate, but they require a measured accuracy and update-rate trial in the actual building, including doorway and non-line-of-sight cases. A software-only monocular SLAM pipeline from the downlinked Mini 3 video is useful as a research track, not the initial safety source: DJI specifies approximately 200 ms minimum live-view latency, and the published Mini 3 MSDK material does not establish synchronized raw camera and IMU access. [Mini 3 specifications](https://www.dji.com/mini-3/specs)

For autonomous exploration of an initially unmapped office, the practical long-term option is an aircraft that exposes onboard VIO/depth or supports an onboard payload computer and directional sensors. DJI's current Payload SDK supported-product list covers enterprise aircraft, not Mini 3. [Payload SDK supported products](https://developer.dji.com/doc/payload-sdk-tutorial/en/index.html)

## Staged acceptance

### M1: one-drone room capture

- Safety pilot places one guarded Mini 3 at the approved pose.
- Sweep receives a spoken or gesture-derived `capture_room` intent and shows the plan for confirmation.
- The bridge reports fresh flight, battery, link, camera, gimbal, and storage state.
- Sweep holds position and executes the verified capture pattern.
- Media retrieval and a private Marble job complete with provenance.

### M2: pilot-assisted multi-room survey and capture

- The RC safety operator flies through 3 to 5 rooms while Sweep records room-entry, doorway, candidate-pose, and capture events.
- Run `capture_room` at each approved pose and preserve both sides of every doorway.
- Without an accepted shared position source, label the result as a topological room graph and keep it out of autonomous planning.
- The pilot-assisted result may produce the complete visual walkthrough before autonomy is ready.

### M2 autonomy gate: one-room autonomous motion

- Add one external shared-pose source and directional clearance coverage in a contained room.
- Prove hover, translate, yaw, stop, and return-to-launch-pad routes on one aircraft.
- Measure pose error, command latency and jitter, tracking error, stopping distance, and every stale-source behavior.

### M3: known-map multi-room capture

- Extend tracker and clearance coverage through open doorways and all test rooms.
- Import a surveyed occupancy map and approve room poses.
- Pass one aircraft before two, then three; serialize doorway traversal.
- Marble stays downstream and never supplies localization, geometry, or collision evidence.

### Future: initially unmapped exploration

- Move to an aircraft with onboard VIO/depth and payload integration, or accept the cost and fragility of building-wide external tracking.
- Build conventional SLAM and frontier exploration first. Use Marble only to create the visual walkthrough after safe capture.

## Phase decision

The Mini 3 stack can credibly deliver the M1 drone-capture experience and a controlled known-map autonomy demonstration. It cannot credibly promise general indoor autopilot from bundled sensors alone. The purchase decision therefore does not remove the localization and obstacle-sensing work; it makes those the explicit gates between stationary room capture and autonomous room-to-room flight.
