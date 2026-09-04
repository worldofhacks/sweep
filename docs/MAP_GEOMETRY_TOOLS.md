# Offline geometry authoring

`tools.map_geometry` produces conservative occupancy grids and reports whether a
proposed route tube and two formation boxes fit the supplied map evidence. Run it
on a laptop or development server. Outputs always carry `status: offline_authoring`
and `flight_approved: false`; they cannot authorize a flight.

## Run the example

```bash
.venv/bin/python -m tools.map_geometry \
  tests/fixtures/geometry \
  tests/fixtures/geometry/geometry_authoring.json \
  /tmp/sweep-geometry-example \
  --accepted-version synthetic-geometry-v1
```

Use a fresh output path each time. The tool refuses an existing directory. The
example reports a clear route, a candidate kitchen box, and a rejected atrium box.
The fallback is kitchen-only use if later accepted. Open the generated
`preview.html` in a browser to inspect each altitude band, tag axes, scan points,
the route tube and formation boxes. An elevation projection shows height bounds.

The fixture is synthetic. Its free-space declaration is an explicit test input;
its sparse three-point cloud cannot establish that a physical room is empty.
The sample polygons have no owner approval.

## Prepare real inputs

Start with the validated bundle from [the survey tools](MAP_SURVEY_TOOLS.md).
`geometry_authoring.json` is a separate, editable authoring request pinned to that
bundle's `content_sha256`. Copy the example structure and replace its geometry:

1. In CloudCompare, export each registered source as **ASCII PLY** with XYZ vertex
   properties. The reader accepts ASCII PLY 1.0, including extra scalar vertex
   fields such as RGB. Binary PLY, OBJ, GLB and other phone export formats must
   first be opened in CloudCompare and re-exported with the PLY ASCII option.
   Preserve scan-local coordinates and the saved `T_map_scan`; exporting already
   transformed building coordinates requires an identity transform to avoid
   applying registration twice. Update the bundle source hashes after conversion.
2. Include every pinned source scan in `cloud_sources`. Omitting a scan or using
   an empty cloud list is rejected. This first adapter requires all manifest
   sources to be readable ASCII PLY scans. Source vertices are transformed into
   the building frame with the saved registration matrices.
3. Set the graph `floor_id` and its surveyed `floor_elevation_m` in the building
   frame. Supply an XY flight box and the closed surveyed wall boundary, with
   `wall_source` identifying its pinned scan. These are operator-authored inputs;
   the tool does not reconstruct wall geometry from sparse points.
4. Supply explicit observed free-space volumes, each linked to a pinned source,
   with a closed XY polygon and absolute building-frame `z_min`/`z_max`. Set
   `evidence_kind: surveyed` for real evidence. Such a volume becomes eligible
   only when both `observed` and `owner_approved` are true. Missing or unapproved
   space stays blocked. A flag records the operator's declaration; it does not
   replace a survey or prove that its contents are accurate.
5. Complete `obstacles.yaml` for glass, counters, steps, railings and hanging
   fixtures. Add every excluded region in `no_fly`, including stairs, elevator,
   mezzanine, enclosed rooms and unaccepted branch coverage. No-fly volumes use
   absolute building heights. Obstacle floor labels never suppress physical
   collisions: any altitude-overlapping obstacle contributes.
6. Author the route centerline, lateral `half_width_m`, and absolute height
   interval. Author axis-aligned kitchen and atrium boxes inside their matching
   pinned zones. Supply measured/planned `separation_m`, `stopping_m`,
   `p95_error_m` and guarded `drone_radius_m`. Regenerate and review the report.

This authoring tool assumes the supplied wall, free-space and no-fly inventories
are complete. Measured inventory review remains a required physical gate. Unknown
space cannot be cleared by an absent voxel or an empty point-cloud region.

## Geometry rules

The grid has 0.10 m cells at 0.8, 1.2, 1.6, 2.0 and 2.4 m above the declared floor.
Rows advance in +y and columns in +x from `origin_xy`. A value of 1 means blocked
or unknown; 0 means a candidate for later acceptance.

A complete cell must lie inside the flight box and bundle geofence and at least
1 m inside the surveyed wall boundary. Threshold uncertainty rejects a boundary
cell. The exported geofence is a conservative collection of inset raster cells,
rather than an exact offset polygon. Its height bounds come from the map bundle.

Each candidate cell's footprint and height are expanded by 0.75 m and must fit
inside one eligible free-space volume. Adjacent evidence volumes are treated
separately, so an unproven join remains blocked. Each observed point occupies its
entire 0.10 m voxel. Voxels, obstacle primitives and no-fly volumes block cells
within 0.75 m horizontally and vertically. This uses a conservative product of
horizontal and vertical margins; it can reject more space than a spherical margin.
Cloud cropping retains points just outside the flight box that could affect it.

The route check covers every cell touched by its swept XY tube and checks the
entire requested altitude interval. A route extending outside the grid fails.
It may reject a narrow valid corridor because touching a blocked cell counts as
collision. Route endpoints and the connecting graph still need operator review.

Formation checks cover the whole rectangular box, its continuous altitude range,
and containment in the corresponding named zone. The static two-drone fit uses
`envelope = stopping_m + p95_error_m + drone_radius_m`. Center separation must be
at least twice that envelope. The long box dimension must fit the separation plus
two envelopes; the short dimension and height must each fit two envelopes.
Trajectory entry/exit, fleet control, motion dynamics and measured stopping
performance require downstream integration and acceptance.

Tag proximity samples the route centerline at intervals of at most 0.10 m at both
altitude endpoints and records distance to the nearest same-floor tag. Distances
beyond 2.5 m flag candidates for inspection. This is explicitly
`candidate_proximity_only`, with `visibility_verified: false`. It does not prove
coverage across the tube or between samples. FOV, printed orientation, occlusion,
gimbal attitude, lighting and the measured 720p detection envelope remain open.

## Artifacts and limits

| Output | Contents |
|---|---|
| `grid_<floor>_<band>.npy` | NumPy v1 uint8 array; 1 blocked/unknown, 0 candidate. Written with NumPy save; readable with NumPy load and pickle disabled. |
| `geofence_<floor>.json` | Inset cell rectangles, source wall polygon, absolute height bounds and version metadata. |
| `geometry.json` | Bundle version/content hash, authoring hash, grid coordinates, output file hashes, route report, tag-proximity samples and formation decisions. |
| `preview.html` | Self-contained plan/elevation views for review. |

Keep the directory together. `.npy` files alone do not contain the map identity;
`geometry.json` supplies that identity and hashes every generated artifact. Hashes
record content integrity and do not authenticate a survey or approve a flight.

The initial authoring adapter caps each cloud at one million vertices, the grid
at 100,000 cells, and routes at 1,000
points and 1,000 m total length. Crop or split larger surveys. This implementation
indexes altitude-relevant voxels into nearby one-meter XY bins. A 10 × 10 m
room with 10,000 floor voxels runs in the regression suite and checks both blocked
and free results. Open3D/binary-cloud import and
large-site processing remain follow-on work.

## Physical acceptance still required

Review the real wall and obstacle inventories and approve the lobby, kitchen and
atrium polygons on the annotated plan. Measure furniture and overhead fixtures,
report the atrium go/no-go, and check 6–10 held-out physical checkpoints within
0.10 m. Validate delivered-camera tag visibility along the route, then integrate
version pinning, clearance, stopping bounds and the live safety drills. None of
those acceptance gates can be earned by this synthetic authoring run.
