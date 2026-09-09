# Approved wall inputs for the flight map

The owner approved the tag map and six measured wall segments on 9 September
2026. `flight-map-approval-20260909.json` pins the exact source files.
`flight-map/flight-wall-inputs.json` prepares those walls for flight-map assembly;
`flight-map/console-wall-features.json` provides the matching console polygons.
The combined overview is `measured-walls-lidar-overview.png`. Ground robots
continue using their live LiDAR.

The flight map still needs calibration and physical route evidence before it can
be loaded. The retained map uses tag 38 as its origin and tag 38 toward tag 39 as
positive X. The current world-bundle validator requires a tag-0 datum and its
canonical world axes. Preserve the retained coordinates and supply an explicit
registration transform when assembling the bundle.

Regenerate the prepared inputs into a new directory:

```sh
uv run python -m tools.prepare_flight_wall_inputs /tmp/sweep-flight-wall-inputs
```

After measuring the world registration, pass `--world-transform /path/to/transform.json`.
That JSON record must contain `sourceFrame`, `targetFrame`, `transformId`,
`T_world_source`, and `evidence`. Use `unit11_atrium_38_to_39_v1` as the source and
`world` as the target. The tool checks a rigid Z-up transform and emits a v2
`obstacles.yaml`; the world-bundle validator must still verify its physical datum
and registration evidence. Apply the same transform to tags, destination geometry
and occupancy registration during bundle assembly.

The post-calibration session must complete these inputs:

1. Measured grid/route boundaries, free-volume heights, arrival and home slots,
   and aircraft clearance allowances. The six estimated 2 m wall extents provide
   only part of the room boundary.
2. Camera calibration and visibility envelope, camera-to-body transform,
   localization registration, source identities and clock mappings. Exclude
   damaged tags 35 and 49 from localization.
3. Independent tape evidence and held-out checkpoints required by the current
   map and geometry validators.
4. An accepted world bundle containing the transformed walls, generated
   navigation grids, and a deployment signed for the actual session and aircraft
   epochs. Load it through `SWEEP_NAVIGATION_CONFIG` and verify route rejection
   through the measured walls before enabling the flight test.

The console's map approval label records source-map acceptance. The running
relay remains on its existing supervised configuration while calibration is in
progress. The gray historical LiDAR is an offset visual reference; it supplies
no flight clearance. Existing 7 ft soft and 8 ft hard limits remain policy limits.
