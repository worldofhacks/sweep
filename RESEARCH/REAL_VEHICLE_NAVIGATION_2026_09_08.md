# Real-vehicle navigation: hardware-boundary review

8–9 September 2026. Audience: the engineers preparing the two Mini 3 aircraft and
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
wrong body-frame command when yaw is stale. The repair carries availability
and monotonic receipt time through the snapshot conversion and rejects or holds
mapped movement when either sample exceeds the existing freshness policy. LAND
and emergency control retain priority. Regressions exercise the snapshot conversion
and the navigation controller, including missing and stale heading or velocity.

Position timing has a separate limitation. Android retains `positionMeasuredAtMs`,
but its telemetry frame carries publication time. The relay compares that position
with a localization pose evaluated at its mapped time. At the test fixture's
0.2 m/s speed and 5 mm tolerance, a 25 ms sample offset can consume the whole
tolerance. Field settings are still pending measurement, so this does not establish
a field failure rate. Measure moving-stream disagreement and choose a tolerance
within the reserved tracking allowance. A time-alignment calculation would first
need trustworthy acquisition times for both positions.

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
retired wire state while leaving the mission waiting. The repair reports an explicit
failed lifecycle and independently publishes HOLD, including when the route
publisher has already removed its active entry. Cancellation retires every pending
command owned by the intent. Late acknowledgements cannot restore a cancelled owner.

Callback ordering also matters. A multi-stop workflow can hold its lock while
waiting for command publication. Synchronously reporting back into that workflow
from the publisher or a late acknowledgement created a lock cycle. Terminal
callbacks now run after critical command publication. Another regression covers
resume snapshot acquisition outside the non-reentrant owner lock, with ownership
checked again before retaining the next command.

The adapter socket also awaited resumed route execution before reading its next
frame. When a completed segment triggered another GOTO, that GOTO waited for an
acknowledgement on the blocked socket and timed out. The relay now commits and
publishes the acknowledgement, claims the continuation, and resumes it as tracked
background work. A real-socket regression proves that the next command reaches
execution and retains its owner.

## Coherent navigation observations

The relay previously copied fleet state and then read the changing localization
registry during navigation checks. A newly admitted pose could appear to come from
the future relative to the copied state. Arrival checking could also combine the
coordinates of one pose with the timestamp of another and accept an unproven arrival.

The relay now copies fleet state and current-epoch control poses under one lock.
Navigation validation, arrival checks and initial wire publication use the captured
poses. Callers without a captured map sample each pose once per check. Regressions
exercise both races, an empty captured map, and immutability of the copied mapping.
This consistency fix leaves physical sample-time alignment as a separate field
measurement requirement.

The final HOVER could also finish before another localization sample arrived.
GOTO still requires a position observation captured after dispatch. The following
HOVER validates the proven arrival position, current hover state and its own
completion window without requiring a new observation of unchanged position.

Route preparation and tracking publication now serialize evidence creation and
audit admission. Previously, a tracking update could enter the audit before an
older route authorization and cause the authorization to be rejected. HOLD does
not wait on this publication lock. Network delivery can still reorder old-route
poses; an Android socket regression verifies that admission rejects those poses
without clearing the replacement route's position or preventing its GOTO.

The loopback device also needed consistent telemetry and localization publication.
Its navigation fixture now advances through positions and derives localization from
relay-admitted telemetry. Profiling found that its attempted 50 Hz localization
stream consumed nearly the entire serialized relay processing budget before node
telemetry and status updates. The bounded rehearsal uses 10 Hz localization and
0.04 m/s motion, with matching signed timing limits and the same 5 mm position
checks. Field configuration must account for aggregate device update rates and
measured end-to-end evidence age. Production continues rejecting stale or
conflicting positions.

## Search dispatch authority

The empty-survey run exposed a missing command scope in the SEARCH execution path.
Its GOTO reached the remote adapter without the frozen navigation plan required to
sign the route. The adapter rejected it and the controller issued a safety HOLD.
The previous integration test accepted any command with the mission ID, so that
HOLD satisfied its assertion.

SEARCH now dispatches within the frozen plan's navigation scope, using the current
coherent snapshot provider. The regression requires signed route authorization and
pose evidence before GOTO. It acknowledges the command, waits for retained execution
ownership, then fails the camera worker and requires a separate safety HOLD. Late terminal
SEARCH results also finish the coverage runtime and retire detection workers. That
cleanup runs outside the relay mutation lock so joining a camera worker cannot
block a worker that needs the same lock.

Coverage activation also compared raw adapter ENU coordinates with world-frame
task positions. A non-identity deployment could finish its route without activating
any camera tasks. Activation now converts the aircraft position through its
configured navigation frame before comparing it with the planned task location.
The dispatcher now notifies coverage activation after validated asynchronous
navigation completion as well as synchronous completion. Both paths preserve
position checks; the asynchronous callback also verifies retained mission ownership.

## Camera readiness after navigation

The relay accepts camera readiness for five seconds. The Android bridge previously
sent readiness only on join, a state change or an explicit camera operation. A
camera whose state stayed ready therefore became unusable after a longer route.
Periodic readiness refreshes preserve the existing freshness check while allowing
photos at later stops. The test node follows the same refresh contract.

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
