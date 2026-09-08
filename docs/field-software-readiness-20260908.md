# Field software readiness, 8 September 2026

The software includes route planning and confirmed execution, authenticated aircraft
and ground observations, map and geofence evidence tooling, localization admission,
flight evaluation, and MCAP replay. Physical acceptance remains open.

The first autonomous ground capture exposed LiDAR and recovery defects. The repairs
retain typed scan failures and their raw evidence, bind any self-return profile to
the measured device and mount, and preserve the live owner's odometry and deadline
through repeated pauses. The self-return profile stays inactive until qualified on
hardware. A separate inspected-forward mode accepts one reviewed image challenge
for one bounded pulse. It retains the full-circle LiDAR and lease checks. See the
[positioning guide](OHMNI_CAMERA_POSITIONING.md) for the operator procedure.

The [manual mapping record](mapping-session-20260908.md) pins the complete 53-tag
map, the held-out camera pose, and the provisional wall overlay. Both camera
intrinsics and the mount still need qualification before camera-derived flight
localization can use those measurements.

## Physical work, in order

1. Establish the Mini 3 bridge and WHEP feed (#43, #51), then measure exact
   1280x720 intrinsics and latency (#83).
2. Run the Ohmni probe (#247), qualify the LiDAR profile and stop behavior, tape-tie
   the tags (#78), and approve the Level 1 bundle, routes, and geofence (#81, #82).
3. Measure Ohmni registration and tag ties (#243), validate live localization and
   ground stops (#84, #246), then run Mini 3 axis, deadman, and RC takeover probes
   (#85).
4. Complete one-drone control and five recorded named-route rehearsals (#19, #86),
   then the second aircraft, walking skeleton, and two-drone formations
   (#20, #18, #87).

The [flight evidence guide](FLIGHT_ACCEPTANCE_EVIDENCE.md) defines the measured
inputs for the evaluator. Software tests supply no physical acceptance evidence.
Teammate work on #248 and #143, and shared live occupancy work on #245, remain
outside this integration.

## Mini 3 camera calibration

Use the known Mini 3 lens as an initial estimate for a server-side camera fit.
DJI lists an 82.1-degree field of view, 4K recording, and 720p/30 controller live
view ([camera specifications](https://www.dji.com/mini-3/specs)). The Android
publisher currently selects the 1280x720/30 preset and also has a 1080p preset.
Confirm the decoded stream dimensions and any crop or scaling during capture.

Collect calibration views through that exact feed, fit and validate the camera
model on the host, and retain timing and gimbal attitude with the frames. A fixed
downward gimbal setting simplifies the planned floor-tag workflow. Camera
calibration, camera-to-aircraft geometry, feed delay, and control-axis checks each
need their own measured evidence before camera-derived autonomous flight.
