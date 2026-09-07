# Ohmni tag candidate fusion

`tools/ohmni_tag_candidate_fusion.py` creates one unapproved offline candidate file from canonical observation events. It only estimates XYZ tag centers. It does not approve flight or establish aerial clearance.

The fusion core accepts typed `relay.observations.Observation` values. The command reads hash-pinned evidence below one root, accepts only regular files through no-follow descriptors, limits all evidence together to 10 MiB, and publishes one create-only candidate file. The candidate itself is limited to 1 MiB.

Each usable tag event must have a non-null nanosecond `t_capture`, a matching canonical camera-frame event, and a source-scoped odom-to-body pose within the request's capture-association bound. Source receipt timestamps do not establish capture concurrency. The existing camera-smoke producer records receipt time and a null capture time, so its events remain diagnostic until a measured capture association is available.

The request pins four inputs: canonical observation JSONL, a schema-v2 fisheye calibration artifact, a measured body-to-camera mount, and an `ohmni_world_registration_candidate`. The calibration is checked through `CameraTagDetector`, which applies the same serial, quality, resolution, fisheye-model, and rectification checks as the detector. The registration must bind the matching source scope and `odom` frame to `world`.

For each accepted observation, fusion composes `T_world_odom × T_odom_body × T_body_camera × T_camera_tag`. It requires a positive 3×3 pose covariance and weights translations and rotations by inverse covariance trace. Translation and rotation spread limits remain explicit measured qualifiers. A required independent tape checkpoint compares two fused XYZ tag centers after fusion. Its error bound must be positive and at most 0.10 m.

## Request

The request is JSON with this closed shape:

```json
{
  "schema_version": 2,
  "kind": "ohmni_tag_candidate_fusion_request",
  "source_scope": {"session": "...", "device_id": 9, "connection_epoch": 1, "source_id": "..."},
  "odom_frame": "odom",
  "calibration_id": "sha256:<pinned calibration hash>",
  "maximum_association_error_ns": 1000000,
  "maximum_translation_spread_m": 0.05,
  "maximum_rotation_spread_rad": 0.05,
  "minimum_observations_per_tag": 2,
  "observations": {"path": "observations.jsonl", "sha256": "..."},
  "calibration": {"path": "calibration.json", "sha256": "..."},
  "mount": {"path": "mount.json", "sha256": "..."},
  "registration": {"path": "registration.json", "sha256": "..."},
  "tape_checkpoint": {"schema_version": 1, "kind": "independent_tape_checkpoint", "measurement_kind": "XYZ_euclidean_distance", "tag_ids": [0, 1], "measured_distance_m": 1.2, "maximum_error_m": 0.05}
}
```

The mount has schema version 1, kind `ohmni_camera_mount`, measurement kind `measured_rigid_mount`, its camera serial, `body_frame`, `camera_frame`, and `T_body_camera`. The observation JSONL contains full canonical observation events after relay ingestion, including `t_ingest`.

Run it with:

```bash
uv run python tools/ohmni_tag_candidate_fusion.py request.json evidence/ candidate.json
```

If the source lacks capture association, the resulting unapproved file contains no candidate tags, a non-evaluated checkpoint, and bounded diagnostics. A tape-checkpoint failure prevents publication.
