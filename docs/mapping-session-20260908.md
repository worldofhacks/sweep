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

## Agreement with recorded dimensions

Comparing tag-center distances in the final map against the recorded survey inputs gives these differences. The camera fit has no independent metric qualification, so these differences do not establish measurement error.

| Dimension | Recorded input | Fitted value | Difference |
| --- | --- | --- | --- |
| Tags 19–37 | 629 in | 671.27 in | +42.27 in |
| Tags 31–0 | 122 in | 226.44 in | +104.44 in |
| Tag 15 center height | 56 in | 58.39 in, unconstrained fit | +2.39 in |
| Tag 18 center height | 59 in | 63.31 in, unconstrained fit | +4.31 in |

The hallway list contains 19 tags, including tag 53, which creates 18 gaps. Eighteen nominal 37-inch gaps total 666 inches, 5.27 inches below the fitted length. The recorded 629-inch length equals 17 such gaps. An omitted gap is a possible explanation to check against the physical endpoints.

The corridor-to-carpet cross-tie differs from the recorded input by 2.65 m. After reviewing this comparison, the operator confirmed the provisional map's tag 31–0 placement. Retain that placement and preserve the original 122-inch entry as a conflicting survey input. The confirmation supplies no new tape measurement or independent metric uncertainty. The 16–17 baseline sets the fit's scale; its exact agreement is imposed. The selected tag-18 candidate also uses the measured height as a constraint. Neither supplies independent validation.

The recorded inputs are in `office-tag-layout-provisional-20260908/provisional-office-tag-layout.json`; the wall alternatives are in `unit12-live-camera-map-fit-20260908/wall-height-constrained-alternatives.json`, under the same capture-host root as the artifacts above.

## Physical acceptance still required

Approve the camera intrinsics and fixed-head mount from qualified physical evidence. Validate world ties and independent reference measurements, then approve the map, static flight grid, routes, and geofence through the existing bundle tooling. Exercise the repaired LiDAR guard and stop controls on hardware before relying on them for a new capture run. Camera-derived localization and autonomous flight remain gated by those approvals.
