# Mission runtime configuration

`SWEEP_MISSION_CONFIG` points to a versioned JSON file that enables mapped formations, visual search, or both. If the variable is absent, neither runtime is configured. The file must name every allowed mission zone, camera source, map pin, and motion-bound formation layout. A changed file is read only when the relay starts, so restart the relay after updating it.

`SWEEP_DETECTION_CAMERA_IDS_JSON` binds a physical camera identity to each drone that may supply detection evidence. It is a JSON object such as `{"1":"front-camera"}`. An absent value leaves detection-camera identity integration disabled.

```json
{
  "schema_version": 1,
  "mapped_formations": {
    "permission_zone_ids": ["lobby"],
    "formations": {
      "line": {
        "shape": "line",
        "zone": {
          "zone_id": "lobby",
          "floor_id": "level_1",
          "polygon_xy": [[1, 1], [19, 1], [19, 19], [1, 19], [1, 1]],
          "z_min_m": 0.5,
          "z_max_m": 3.5,
          "owner_approved": true,
          "formation_enabled": true
        },
        "layout": {
          "center": {"x_m": 10, "y_m": 10, "z_m": 1.5, "floor_id": "level_1"},
          "heading_rad": 0,
          "spacing_m": 2,
          "altitude_offsets_m": [0, 0]
        }
      }
    }
  },
  "search": {
    "areas": [{
      "zone_id": "search-zone",
      "floor_id": "level_1",
      "polygon_xy_m": [[1, 1], [10, 1], [10, 5], [1, 5]]
    }],
    "map_pin": {"version": "map-v3", "content_sha256": "<64 lowercase hex characters>"},
    "camera": {
      "horizontal_fov_deg": 90,
      "vertical_fov_deg": 90,
      "height_agl_m": 1,
      "gimbal_pitch_deg": -90,
      "gimbal_min_pitch_deg": -90,
      "gimbal_max_pitch_deg": 0,
      "overlap_fraction": 0.25
    },
    "calibration_id": "search-camera-v1",
    "source_by_drone": {"1": "front-camera"},
    "permission_zone_ids": ["search-zone"],
    "mission_version": 1,
    "maximum_drones": 1,
    "floor_z_m": 0,
    "height_tolerance_m": 0.05,
    "camera_offset_z_m": 0
  }
}
```

The top-level file always contains `schema_version`, `mapped_formations`, and `search`. Set an unused runtime section to `null`. A formation section names only its configured formation zones in `permission_zone_ids`. A search section names only its configured areas in `permission_zone_ids`, maps each available drone to a unique camera source, and uses the same map pin as the navigation runtime. The loader rejects omitted fields, extra fields, duplicate identifiers, nonfinite numbers, malformed polygons, and mismatched map or zone permissions.

The producer must send the configured `source_by_drone` value as `source_id` and the corresponding `SWEEP_DETECTION_CAMERA_IDS_JSON` value as `camera_id`. `floor_z_m` is the mapped floor elevation. `camera_offset_z_m` is the measured vertical offset added to the aircraft body pose to obtain camera elevation. `height_tolerance_m` bounds the permitted camera-height error. Coverage evidence also remains subject to the localization calibration and capture-clock checks supplied by the control-localization configuration.
