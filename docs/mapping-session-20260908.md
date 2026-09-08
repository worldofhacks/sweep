# Manual mapping session, 8 September 2026

The retained map contains all 53 expected tags: IDs 0–53, with 29 intentionally absent. The final coordinate frame uses tag 38 as its origin, positive X toward tag 39, and positive Z upward. Camera observations cover the corridor and both floor grids; the wall-tag alternatives retain their measurement and fit provenance.

This is a private provisional map. Both fisheye calibrations remain unqualified, and the fixed-head body/camera mount remains provisional. The map has no registry or flight authority. Reprojection residuals describe fit consistency; they do not establish survey accuracy or clearance.

## Retained artifacts

Paths are under `/var/tmp/gauntlet/sweep-production/` on the capture host.

| Artifact | Location | SHA-256 |
| --- | --- | --- |
| Complete tag map | `unit12-live-camera-map-fit-20260908/final-53-tag-map.json` | `7f4f1f2bf3e2ba0285568a7a0a78bd25587fcba82b5b31a97b4c592b00ca2009` |
| Held-out Position 12 camera pose | `unit12-live-camera-map-fit-20260908/unit11-position12-frozen-final-map-pose.json` | `70dad96579e9bd3729dff0256ced81c9bd4fe008aab5cadc53503f0f5c87fa0f` |
| Provisional wall overlay | `unit12-live-lidar-wall-map-20260908/unit11-provisional-tag-wall-overlay.json` | `8ffc9dfdf165d5199fe407a88258a6dc273c5877d859304fe107667071009327` |

The map and wall overlay have adjacent PNG previews. The wall overlay contains 43 supported segments from 12 Unit 11 body poses. It retains the ambiguity of raw LiDAR registration and does not approve a new sensor mount.

Validation checked all 95 referenced source hashes, finite numeric values, and the expected tag set. The historical 11-position capture index is preserved as an immutable snapshot. The live index includes the later Position 12 capture. Rebuilding the snapshot references changed artifact hashes while preserving tag geometry, wall-tag choices, camera transforms, and wall segments.

## Physical acceptance still required

Approve the camera intrinsics and fixed-head mount from qualified physical evidence. Validate world ties and independent reference measurements, then approve the map, static flight grid, routes, and geofence through the existing bundle tooling. Exercise the repaired LiDAR guard and stop controls on hardware before relying on them for a new capture run. Camera-derived localization and autonomous flight remain gated by those approvals.
