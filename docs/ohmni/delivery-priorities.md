# Mixed-fleet delivery priorities

The product goal from the host owner is one console for aircraft and Ohmni robots:
individual and group selection, manual buttons, gestures, coordinated motion, live
video, and truthful sensing. The original pinned contract is preserved verbatim for
provenance. Its design choices differ from the current issue #239 dependency chain.
This document records that difference; it does not replace the owner's instructions.

## Recommended delivery foundation

1. Finish and independently verify stop, watchdog, acknowledgement, and motion
   admission behavior. Keep hardware findings from the actual Android robots.
2. Establish the explicit frame and shared observation boundary in
   [#94](https://github.com/worldofhacks/sweep/issues/94), then adapt the relay and
   console device-class foundations in #241 and #242 to that contract.
3. Ship a focused Ohmni runtime under
   [#246](https://github.com/worldofhacks/sweep/issues/246), reusing only the protocol
   code exercised by the existing fake aircraft and Ohmni node. Prove one robot's
   drive, stop, camera, lidar mounting, and launch pose with hardware evidence under
   [#247](https://github.com/worldofhacks/sweep/issues/247).
4. Register the shared world frame (#243), approve the static map and zones (#248),
   and route class-aware navigation through that approved world (#249). Fresh live
   observations may veto motion under #245; they do not authorize new free space.
5. Deliver the owner's gesture and coordinated-group controls on the same confirmed
   intent path. The current fixed-demo epic explicitly excludes gestures and ground
   formations, so those product requirements need explicit follow-on acceptance.

The first operational milestone is one aircraft and one robot selected and controlled
individually from the same console, with verified stops, live feeds, and positions in
the same measured room frame. Then increase the fleet and exercise coordinated paths.

## Decisions to reconcile before production merge

| Preserved integration design | Current issue-chain design | Consequence |
| --- | --- | --- |
| Dedicated lidar sensor message; pose frame is implicit | #94 shared observation envelope and explicit frames | Migrate the transport and its vectors together across relay, node, and console. |
| Resettable live occupancy raster used only for display | #248 approved static map plus #245 expiring blocked-cell overlay | The live raster does not satisfy map approval, registration, or route veto acceptance. |
| Shared node runtime plus class-specific device implementations | #246 focused runtime with exercised reusable protocol code | Reuse proven clock, ACK, watchdog, and hardware behavior without requiring the entire integration branch to land. |
| Floor-plane group formations and translations | #249 approved-map named-zone routes; current epic defers ground formations | Preserve the requested controls as product goals and make their release gates explicit. |

The newer issue text is not a substitute for hardware evidence. In particular, #247's
ROS/container references must be reconciled with the measured Android deployment:
there is no Docker on the three robots in the handoff. G-01 and G-02 have lidar kits;
G-03 does not. No software change can provide a scan on absent hardware.

## What has and has not been delivered

- Contract, maps, scratchpad behavior, and private configuration are preserved.
- The integration branch contains implemented controls, class-aware planning,
  sensing displays, node packaging, and regression tests. PR #255 is closed.
- Those changes have not been merged into main, installed on a robot, or accepted
  as physical motion evidence. Automated tests and simulated socket roundtrips do
  not establish lidar alignment, odometry closure, room registration, or clearance.
- The live relay, console, robots, and unrelated port 8000 have not been replaced by
  this integration work.
