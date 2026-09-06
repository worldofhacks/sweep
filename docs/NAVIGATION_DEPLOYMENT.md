# Navigation deployment

`SWEEP_NAVIGATION_CONFIG` enables a configured navigation runtime. It points to one JSON file that pins the accepted map bundle, generated geometry, arrival slots, motion limits, and aircraft limit. The loader rejects unknown fields and treats a changed configuration file as a revoked deployment.

The default `synthetic` backend supports simulator demonstrations. The `remote` backend requires surveyed geometry and a matching navigation-evidence file. Remote route commands carry a route identity for the phone-navigation adapter. Physical dispatch also depends on the existing control-localization and adapter gates.

## Configuration

The JSON document uses schema version 1. Paths resolve relative to the configuration file. `accepted_versions` maps each accepted map version to its SHA-256 digest. Every arrival slot belongs to a configured zone, and `permission_zone_ids` is intersected with zones whose `navigation_allowed` flag is true.

```json
{
  "schema_version": 1,
  "bundle_directory": "map-bundle",
  "geometry_directory": "geometry",
  "accepted_versions": {"building-v1": "<64 lowercase hex characters>"},
  "zones": [{"id": "atrium", "floor_id": "level_1", "navigation_allowed": true, "aliases": ["atrium"], "arrival_slots": [{"id": "atrium-1", "x_m": 2.0, "y_m": 1.0, "z_m": 1.8, "radius_m": 0.4, "half_height_m": 0.3}]}],
  "permission_zone_ids": ["atrium"],
  "execution": {"floor_id": "level_1", "motion": {"aircraft_radius_m": 0.15, "aircraft_height_m": 0.2, "map_uncertainty_m": 0.02, "pose_uncertainty_m": 0.02, "tracking_allowance_m": 0.1, "stopping_allowance_m": 0.1}, "speed_m_s": 0.5, "position_tolerance_m": 0.05, "position_max_age_ms": 500, "minimum_position_quality": 0.5, "segment_timeout_ms": 1000},
  "max_aircraft": 1,
  "control_store_identity": "control-store-v1",
  "evidence_file": "navigation-evidence.json"
}
```

The remote evidence pins the map and geometry digests, repeats the active motion and aircraft limit, and records localization, probes, owner attestation, and one-aircraft completion evidence. Changing that file revokes remote dispatch acceptance.
