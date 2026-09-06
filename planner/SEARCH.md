# Confirmed visual search preview

`SearchPlanner` turns one operator-confirmed request into a frozen, inspectable search
preview. The request pins the map, roster, camera configuration, mission version and epoch,
selected camera sources, and one enabled COCO class: `backpack`, `bottle`, or `suitcase`.
The preview records the map and geometry pins, camera footprint policy, balanced allocation,
coverage lanes, and a sequential transit route for each selected aircraft.

The planner takes known free occupancy cells inside the supplied zone polygon. It accepts one
connected region and assigns contiguous vertical slices with workloads differing by at most one
cell. Each drone's transit is planned through `NavigationPlanner` with the other aircraft held as
stationary reservations.

`CameraPolicy` requires calibrated horizontal and vertical FOV, height above the floor, measured
gimbal limits, a nadir pitch of -90 degrees, and overlap. It derives the ground footprint and lane
spacing. The preview limits coverage targets to known free cells inside the requested polygon.

`SearchPreview.ledger()` returns a `CoverageLedger`. A task starts as `pending` and becomes
`active` when its route is ready. Coverage advances only from fresh `detections` or `empty`
processed-frame events whose source, frame, composite mission identity, connection epoch, and pose
evidence all match the task. Arrival and a sighting alone do not count. Sighting events upsert a
deduplicated candidate only after that frame was accepted for coverage. `hold`, `cancel`, and
`incomplete` preserve the current allocation; an incomplete task emits
`requires_fresh_confirmation: true` for a new plan.

`relay.search_runtime.SearchRuntime` turns that frozen preview into the guarded navigation plan
used by `NavigationRuntime`. Its configuration names the approved polygon and floor, pinned map,
camera calibration, per-drone frame sources, and travel permission. It also requires an explicit
floor elevation, measured body-to-camera vertical offset, and nonnegative height tolerance. A
search is refused unless each selected camera altitude equals `floor_z_m + height_agl_m` within
that tolerance; processed frames receive the same check before coverage can advance. It plans every
lane endpoint through the navigation map before dispatch. A task activates only after the command
reaching its first coverage point has fresh hover, position, and camera-ready evidence. Finishing
the route without accepted processed frames marks the unfinished task `incomplete`; cancellation,
hold, or a new connection epoch never reallocates it.
