# Real-vehicle navigation: hardware-boundary review

8 September 2026. Audience: the engineers preparing the two Mini 3 aircraft and
two Ohmni robots for checkpoint, photo-inspection and search/survey tests.

The existing lateral-flight control should be reused. The remaining work is to
connect measured map geometry and current localization to that control, preserve
mission identity through the relay, and make every failure terminate visibly and
stop motion. The owner has approved the saved 53-tag baseline. Wall and height
measurements and verification of replacement tags remain physical inputs.

This review covers the real console, relay and adapter paths. It excludes new
formations, natural-language work, mixed-fleet allocation and simulator product
features. The implementation and test results are recorded with the
[hardware checkpoint](../docs/hardware-navigation-checkpoint.md).

## Existing lateral flight and mapped flight

The operator confirms that a drone completed lateral flight. That demonstrates the
existing controller/bridge path on hardware. A mapped mission additionally needs
the aircraft's map-frame position, the transform into its local flight frame,
current heading and velocity, and clearance for the complete swept route.

DJI's virtual-stick interface is the underlying actuator path. Its documentation
recommends sending advanced commands at 5–25 Hz and describes conditions that
return control to the RC. The current supported-aircraft list for obstacle
avoidance in virtual-stick mode excludes Mini 3. A successful command send
therefore cannot establish either continuing SDK authority or obstacle avoidance.
[DJI IVirtualStickManager reference](https://developer.dji.com/api-reference-v5/android-api/Components/IVirtualStickManager/IVirtualStickManager.html).

The code already separates the navigation frame from adapter ENU and converts the
route before signing aircraft commands. The hardware test must validate that
transform against observed movement. Replacing it with an identity matrix to make
a test easier would invalidate that evidence.

## Fresh position does not establish fresh heading

The Android review found that `AircraftSnapshot` carried attitude and velocity
availability and timing, but `FlightExecutor` dropped that information while
constructing `AircraftFacts`. Mapped control then consumed `yawDeg` and `speedMS`
alongside fresh position evidence. Missing or stale samples could look like a
zero-degree heading or zero speed.

This is a concrete control defect: a valid world-frame displacement can become the
wrong body-frame command when yaw is stale. The repair must carry availability
and monotonic receipt time through the actual snapshot conversion, and reject or
hold mapped movement when either sample exceeds the existing freshness policy.
LAND and emergency control must retain priority. Tests need to call that conversion
and the navigation controller together; constructing already-valid facts would
miss the defect.

## Mission identity and termination

The multi-stop workflow retains a semantic child intent for each route. The relay
executes the corresponding frozen preview under a `platform:` intent ID. The
review found that completion and failure callbacks used the execution ID while
the photo workflow awaited the semantic ID. The first stop could remain in
`navigating` indefinitely after the child had terminated.

The repair binds those identities at reservation/dispatch and translates the
terminal callback. The regression crosses the real relay WebSocket and signed
navigation-command boundary. It checks both completion and adapter failure.

A separate failure path caught a rejected control-pose publication, logged it and
retired wire state while leaving the mission waiting. The required outcome is an
explicit failed lifecycle plus an independent stop, including when the route
publisher has already removed its active entry. Tests must observe the stop on
the adapter connection and verify that no later mission leg starts.

The loopback test device moves instantaneously and originally published its
telemetry later than the localization fixture. Those contradictory streams explain
some rehearsal failures. A test fixture may order its own evidence consistently;
production must continue rejecting conflicting positions. Ignoring every position
disagreement would hide a real localization defect.

## Camera geometry, time and search results

OpenCV's PnP result transforms object/world points into the camera frame. Its camera
axes point right, down and forward. The inverse gives the camera pose in the map;
composing it with the measured camera/body extrinsics gives the body pose. Square
marker estimation also requires the documented corner order and can have multiple
pose solutions. These conventions matter when a replaced tag has a different
printed orientation.
[OpenCV 4.13 PnP documentation](https://docs.opencv.org/4.13.0/d5/d1f/calib3d_solvePnP.html).

DJI specifies a 720p/30 controller live view and approximately 200 ms minimum
transmission latency for Mini 3; actual delay depends on the setup. Its camera sits
on a three-axis gimbal. Aircraft position and compass heading alone do not locate
a camera ray in the world.
[DJI Mini 3 specifications](https://www.dji.com/mini-3/specs).

The inspected decoded-frame callback exposes image-buffer geometry and format,
without an exposure timestamp. Callback receipt must remain distinguished from
capture time. Preserve the existing calibrated time mapping and uncertainty
instead of assigning the current aircraft pose to an older image.
[DJI CameraFrameListener reference](https://developer.dji.com/api-reference-v5/Components/IMediaDataCenter/ICameraStreamManager_CameraFrameListener.html).

Search results can validly contain class, confidence and image coordinates while
map position is unavailable. A target-free survey must complete when the planned
coverage is observed, even if the detector reports no objects. An unavailable
camera or incomplete coverage must retain its own failure state. For multi-stop
photos, completion requires the retrieved image associated with each arrived stop.

## Static clearance and live obstacles

Mini 3 specifies downward vision sensing. Its hardware specification does not
supply forward wall ranging. Static map clearance therefore depends on the supplied
wall, obstacle and height measurements and current localization. Desktop object
search over delayed images does not establish dynamic collision protection.
[DJI Mini 3 sensing specifications](https://www.dji.com/mini-3/specs).

Nav2's collision monitor illustrates a separate control-layer approach: sensor
observations can stop or limit velocity independently of the route planner, and
stale sources stop motion. Its documentation also distinguishes this CPU-based
mechanism from certified real-time safety. This is supporting design evidence;
the checkpoint does not add Nav2 or claim its behavior for Sweep.
[Nav2 collision monitor](https://docs.nav2.org/rolling/configuration_and_development/configuration_guide/core_servers/collision_monitor/configuring_collision_monitor_node/).

Ohmni documents that `manual_move` continues until explicitly stopped with zero
wheel speeds. Ground cancellation must consequently reach the robot's local stop
path even if a route expires or its remote owner disappears. The existing node
watchdog and LiDAR checks remain essential hardware-test cases.
[Ohmni NativeJS command reference](https://docs.ohmnilabs.com/nativejs/).

## Evidence limits and research completion

Primary SDK, camera, geometry and robot-command references were checked against the
production code. Follow-up inspection concentrated on stale heading/velocity,
frame conversion, capture-time semantics and terminal callback ownership. No new
hardware readings were collected during this software review. The callback and
telemetry contracts explain the identified defects without requiring a new
controller or robotics framework.

Further general searches would not resolve the remaining uncertainties: exact
camera calibration, delivered latency, measured clearance, replacement-tag
placement and behavior of the connected hardware require the upcoming map test.
Automated relay/device substitutes establish software behavior at those boundaries;
they do not measure physical stopping distance, image quality or localization error.
