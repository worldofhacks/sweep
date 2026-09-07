# Ohmni tag candidate fusion

`tools/ohmni_tag_candidate_fusion.py` writes one hash-pinned, create-only, unapproved tag-center candidate from canonical observation events. It does not approve control, flight, or aerial clearance.

## Local odometry candidate

Use the closed schema-v4 `local_odom` request to build a map candidate without a surveyed tag map, a world registration, a vertical datum, or a tape checkpoint:

```json
{
  "schema_version": 4,
  "kind": "ohmni_tag_candidate_fusion_request",
  "candidate_mode": "local_odom",
  "source_scopes": {"pose": {"...": "..."}, "camera": {"...": "..."}, "tag": {"...": "..."}},
  "odom_frame": "odom",
  "calibration_id": "sha256:<pinned calibration hash>",
  "maximum_association_error_ns": 1000000,
  "maximum_translation_spread_m": 0.05,
  "maximum_rotation_spread_rad": 0.05,
  "minimum_observations_per_tag": 2,
  "observations": {"path": "observations.jsonl", "sha256": "..."},
  "calibration": {"path": "calibration.json", "sha256": "..."},
  "mount": {"path": "mount.json", "sha256": "..."}
}
```

Each accepted tag needs a matching camera-frame event and a capture-associated `odom → body` pose from the declared sources. Fusion validates the fisheye calibration and measured `body → camera` mount, then composes `T_odom_body × T_body_camera × T_camera_tag`. It requires valid measured covariance, the requested minimum observations, and the translation and rotation spread gates.

The result has `candidate_mode: "local_odom"`, `candidate_frame: "odom"`, and `approval_status: "unapproved"`. It intentionally omits registration, vertical-datum, and checkpoint fields. A local candidate is not world registered and cannot be used for control approval.

Run it with:

```bash
uv run python tools/ohmni_tag_candidate_fusion.py request.json evidence/ candidate.json
```

## World-registered candidate

The existing closed schema-v3 request remains unchanged. It requires the pinned `ohmni_world_registration_candidate`, optional measured vertical datum, and independent two-tag tape checkpoint. It may emit a world-frame candidate only after those world-evidence gates pass.

Both modes read only hash-pinned regular files under the evidence root, cap total input at 10 MiB, and cap the candidate at 1 MiB.
