# Four-device live demonstration

The target show uses two ground robots and two aircraft in one console on
**http://127.0.0.1:5173/**. Each ground robot contributes two cameras and one LiDAR;
each aircraft contributes one camera and its reported sensors. The aircraft's
owner-reported infrared proximity/depth sensor still needs model and interface
verification. Six camera tiles means six explicitly reported streams, not six
healthy decoders: check source and playback evidence separately.

This is an execution and evidence plan, not a record of completed hardware
acceptance. Software tests never populate the operator runtime. No device, pose,
image, scan, route, progress percentage or successful movement is synthesized to
make the show look complete.

## Console workflow

1. Check the console build identity and relay session. There is one console port,
   5173; relay and media ports are its dependencies. Start with the fleet disarmed.
2. Open **Live → All cameras**. Verify each of the six actual camera identities,
   source freshness, and advancing decoded frames. Focus and inspection are local
   view changes; they do not change the command selection. A reconnect or camera
   remap retires the prior focused identity.
3. Select a ground robot and verify current pose, drive authority, LiDAR clearance,
   local stop and operator readiness. In **Control**, preview a bounded ground
   pulse, inspect its target and duration, then confirm. HOLD and local stop must
   have passed that robot's individual physical acceptance before choreography.
4. In **Gestures**, choose the ground profile and selected ground target. A motion
   gesture creates a preview; the operator confirms it. Return to neutral between
   gestures. Selection, source or epoch changes require a fresh preview.
5. In **Speech**, use the typed command guide for the current fleet. For one ready
   ground robot, explicit phrases such as `robot pulse forward` compile the same
   bounded request. Compiling does not send it. Microphone transcripts require
   an exact qualified relay compiler plan; typing a phrase does not qualify audio.
6. Use the ground survey controls to start one confirmed recording. The relay's
   run ID and epoch establish the lifecycle. Complete or cancel that exact run.
   The console shows receipt-based elapsed time and current scan evidence, not an
   invented collection percentage or count. On completion, load the verified
   occupancy candidate and download the source image/evidence for map authoring.
7. Use **Control → Requests** for session request outcomes. A completed relay
   request and an operator's physical observation are separate evidence. Operator
   notes are explicitly unverified and do not mark the hardware exit passed.

Survey candidates remain in their recorded local odometry frame with
`navigation_authority: false`. Importing an image into the Map editor does not
register it to the world, establish tape measurements, or approve it for flight.
Use the measured registration and approval workflow described in
[platform integration](platform-integration.md).

## Show sequence after individual acceptance

Use a short, deliberate sequence with the camera wall visible throughout:

| Beat | Audience-visible action | Evidence required before this beat |
| --- | --- | --- |
| Fleet reveal | Two ground robots and two aircraft, six advancing camera feeds | Real membership, independent camera publication and decoded playback |
| Ground response | Gesture-selected robot executes a confirmed short move and stops | Per-robot drive, pose, LiDAR, obstacle and deadman acceptance |
| Language response | Typed or separately qualified spoken command controls the second robot | Same command authority and confirmation path; verified input provenance |
| Map reveal | Finish the ground recording and open its real occupancy image | Accepted canonical scans and immutable saved candidate |
| Aerial route | Aircraft follows the measured, approved route | Single-aircraft bridge, axis/deadman, localization and route rehearsal exits |
| Formation reveal | Two aircraft form the approved line; column/transition only after its own release | Paired-aircraft qualification, measured formation volume, supported shape and execution backend |
| Controlled hold | A planned obstacle invalidates the route and the fleet holds | Qualified live obstacle association/veto, local stop/RC coverage, explicit replan with no auto-resume |

The obstacle demonstration uses an object introduced from outside the flight
volume under the physical operator's control. It does not require a person to
enter a propeller path. Keep the flight and ground travel volumes separate until
the shared obstacle and coordinated-motion contracts are qualified.

In the Swarm gesture profile, **Pointing Up** stages the explicit line request;
**Victory** requests the independently advertised formation-next operation. Line
support does not imply column or transitions. The signed mapped-line backend
requires its configured destination and approved common-world geometry. The
general C2 profile is not a shortcut for enabling physical formations.

## Parallel work and twelve-hour checkpoints

Owners below are work streams, not implied assignments to a person. Coordinate
ownership before modifying a teammate's active implementation.

| Stream | Work in parallel | Gate / concrete evidence |
| --- | --- | --- |
| Console integration | Camera wall, confirmed inputs, survey handoff, truthful request evidence | Reviewed source, automated contracts/tests, deployed build identity, real browser checks |
| Ground runtime | #94 → #247 → #246 → #99; both camera publishers, encoder/pose and LiDAR commissioning | Each robot's real scans, pose identity, recording artifact, drive/stop and obstacle-hold report |
| Measured world | #78 → #81 → #248 → #243, then #82 | Tape ties, immutable map, independent registration residual, approved grids/tubes/volumes |
| Aircraft execution | #43 → #85 → #19 → #97, #83/#51/#84, #143–#145, then #86 | Compatible signed bridge, real video/pose, route/hold/failure evidence, five physical rehearsals |
| Two-aircraft formation | #20/#18/#87 and capability release | Both aircraft qualified individually, measured line and column slots, sequential noncrossing transition, physical acceptance |
| Shared veto and evidence | #245, #249 and #93 | Ground route executor, live obstacle HOLD, no auto-resume, retained JSONL/MCAP evidence |

- **Hours 0–2:** freeze source/build/session; assign owners; commission connections,
  cameras, local stop and RC. Report exact blockers rather than guessing values.
- **Hours 2–5:** individual ground pulses and survey artifacts; aircraft single-node
  authority/axis/deadman and measured world inputs in parallel.
- **Hours 5–8:** registered route rehearsals and gesture/typed-input acceptance on
  individual devices; prove HOLD/failure behavior before any concurrent movement.
- **Hours 8–10:** paired-aircraft line, then independently supported column/transition;
  ground route/veto rehearsal in its approved travel volume.
- **Hours 10–12:** rehearse the exact show twice, record evidence, freeze the build,
  and keep only physically accepted beats in the live run.

## Outstanding integration boundaries

At the start of this console package, the platform HTTP navigation review still
reported `dispatchEligible: false`; the separate signed aircraft navigation path
was not a ground route executor. Phone-local directional flight probes did not
implement a relay `body_pulse` intent. Mapped-line support did not implement column
or formation-next, and no generic profile toggle established physical acceptance.

The flight owner must reconcile phone and relay height-policy versions, including
the signed maximum height and local-height freshness fields, using the measured
room clearance. Do not copy a probe's target height over an incompatible deployed
command contract. The ground owner must resolve the actual LiDAR/encoder/pose
findings; a webcam gesture cannot supply missing obstacle evidence.

Record the source revision, session, device identity/epoch, adapter/phone build,
map/registration/configuration hashes, input origin, intent and command IDs,
stop/RC outcomes, and artifact references for each physical run. Keep hardware
issues open until their actual exit criteria pass. See
[unified fleet control](unified-fleet-control.md) for policy and commissioning
requirements and [the laptop guide](laptop-console.md) for deployment.
