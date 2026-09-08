# Coordination integration

This branch connects approved aircraft routes, ground named-zone navigation, and
shared-world recording to the relay's production execution path. It targets
`integration/production-map-flight-20260907`. Console reconciliation and physical
map collection remain separate work.

## Deployment inputs

Aircraft use `SWEEP_NAVIGATION_CONFIG` and the existing approved navigation
artifacts, control-localization bindings, and node admission file. The execution
object now accepts `formation_bindings`, a list of `{shape, zone, layout}` records.
`shape` is `line` or `column`. `zone` contains `zone_id`, `floor_id`, `polygon_xy`,
`z_min_m`, `z_max_m`, `max_speed_mps`, `owner_approved`, `formation_enabled`,
`map_pin`, and `geometry_pin`. Both pins contain `version` and `content_sha256`.
`layout` contains `center: {x_m, y_m, z_m, floor_id}`, `heading_rad`, `spacing_m`, and
`altitude_offsets_m`. These are measured, separately approved formation volumes.
The map's navigation-arrival permission alone does not grant formation permission.

Mapped `line`, `column`, and `formation_next` require confirmation. Aircraft enter
their assigned slots sequentially, with arrival and hold checked before the next
aircraft moves. Every segment and asynchronous resume rechecks the frozen pins,
current poses, and separation. Invalidated plans require a new review.

Ground deployment and measured source bindings are documented in the
[Ohmni runtime guide](../adapters/ohmni/README.md#named-ground-navigation).
Both host and node need the signed deployment and their separately protected
approval key. The host also needs qualified world observations registered to the
same map and transform. Local odometry alone leaves named navigation unavailable.

## Console handoff

The existing destination preview, compile, and confirm endpoints remain the
control boundary. A valid preview retains the seven-field execution identity:
`planHash`, `mapPin`, `geometryPin`, `navigationPin`, `approvalId`,
`configurationSha256`, and `permissionZoneIds`. Routes retain their target class,
epoch, floor, points, and arrival behavior. Ground arrival uses `stop`; aircraft
arrival uses `hover`.

Configured homogeneous aircraft or ground selections can produce executable
previews. Mixed-class selections remain review-only with typed refusal outcomes.
The console must honor `executable` and the returned outcomes. It must never turn
a ground route into an aircraft slot or invent a dispatch from displayed points.
Voice can use the same backend compile and confirmation boundary when its existing
input-channel qualification permits it. This change adds no voice qualification.

## Recording and acceptance

Follow [the replay guide](world-replay.md) for MCAP export and live Foxglove access.
The sidecar runs separately and reads committed audit records. Qualified world
observations record map identity and measured registration evidence; aircraft
delivery records signed-route identity and subsequent navigation pose updates.

The software checks use actual HTTP and node WebSocket boundaries, production
planners and controllers, simulated aircraft acknowledgements, and a fake ground
device. Physical flight and ground-motion qualification still require measured
geometry, registration, stopping clearance, video, and replay of the physical
session. The shared live occupancy producer and fleet-wide obstacle veto remain
outside this branch. Ground motion retains its local full-scan LiDAR interlock.
